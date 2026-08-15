from __future__ import annotations

import json
import os
import time
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ArgoError(RuntimeError):
    pass


POLL_SECONDS = 5
REFRESH_INTERVAL_SECONDS = 30


def _has_revision(sync: dict, revision: str) -> bool:
    if sync.get("revision") == revision:
        return True
    revisions = sync.get("revisions", [])
    return isinstance(revisions, list) and revision in revisions


def _observed_revisions(payload: dict) -> set[str]:
    status = payload.get("status") or {}
    revisions: set[str] = set()
    sync = status.get("sync") or {}
    current = sync.get("revision")
    if isinstance(current, str) and current:
        revisions.add(current)
    extra = sync.get("revisions")
    if isinstance(extra, list):
        revisions.update(item for item in extra if isinstance(item, str) and item)
    operation = status.get("operationState") or {}
    for container in (operation, operation.get("syncResult") or {}, operation.get("operation") or {}):
        if not isinstance(container, dict):
            continue
        value = container.get("revision")
        if isinstance(value, str) and value:
            revisions.add(value)
        nested = container.get("sync")
        if isinstance(nested, dict):
            nested_revision = nested.get("revision")
            if isinstance(nested_revision, str) and nested_revision:
                revisions.add(nested_revision)
    history = status.get("history") or []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            value = entry.get("revision")
            if isinstance(value, str) and value:
                revisions.add(value)
    return revisions


def _ready_state(payload: dict, revision: str) -> tuple[bool, str]:
    status = payload.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    state = f"revision={sync.get('revision')} sync={sync.get('status')} health={health.get('status')}"
    ready = (
        revision in _observed_revisions(payload)
        and sync.get("status") == "Synced"
        and health.get("status") == "Healthy"
    )
    return ready, state


def _request_hard_refresh(kubeconfig: str, application: str) -> None:
    # Ask the application controller to compare against current Git HEAD even when live
    # resources already match after ignoreDifferences.
    # 强制用当前 Git HEAD 做对比；ignoreDifferences 导致无 diff 时也能刷新 revision。
    subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "-n",
            "argocd",
            "annotate",
            "application",
            application,
            "argocd.argoproj.io/refresh=hard",
            "--overwrite",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_application(
    server: str,
    application: str,
    revision: str,
    timeout: int = 600,
    refresh_interval: int = REFRESH_INTERVAL_SECONDS,
) -> dict:
    kubeconfig = os.environ.get("KUBECONFIG", "")
    if kubeconfig:
        deadline = time.monotonic() + timeout
        last_state = "unknown"
        last_refresh = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_refresh >= max(0, refresh_interval):
                _request_hard_refresh(kubeconfig, application)
                last_refresh = now
            result = subprocess.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    kubeconfig,
                    "get",
                    "application",
                    application,
                    "-n",
                    "argocd",
                    "-o",
                    "json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise ArgoError("kubectl returned invalid Argo application JSON") from exc
                ready, last_state = _ready_state(payload, revision)
                if ready:
                    return payload
            else:
                last_state = result.stderr.strip()
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        raise ArgoError(f"Argo application did not become healthy at revision {revision}: {last_state}")
    token = os.environ.get("ARGOCD_AUTH_TOKEN", "")
    endpoint = os.environ.get("ARGOCD_SERVER", server).rstrip("/")
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    last_refresh = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        refresh = now - last_refresh >= max(0, refresh_interval)
        if refresh:
            last_refresh = now
        url = f"https://{endpoint}/api/v1/applications/{application}"
        if refresh:
            url += "?refresh=hard"
        request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urlopen(request, timeout=min(20, max(1, int(deadline - time.monotonic())))) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            last_state = str(exc)
        else:
            ready, last_state = _ready_state(payload, revision)
            if ready:
                return payload
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise ArgoError(f"Argo application {application} did not become healthy at revision {revision}: {last_state}")
