from __future__ import annotations

import json
import os
import time
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Service


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


def _summary_images(payload: dict) -> set[str]:
    status = payload.get("status") or {}
    summary = status.get("summary") or {}
    images = summary.get("images") or []
    if not isinstance(images, list):
        return set()
    return {item for item in images if isinstance(item, str) and item}


def _ready_state(payload: dict, revision: str, expected_images: tuple[str, ...] = ()) -> tuple[bool, str]:
    status = payload.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    observed_images = _summary_images(payload)
    state = f"revision={sync.get('revision')} sync={sync.get('status')} health={health.get('status')}"
    if expected_images:
        state += f" images={','.join(sorted(observed_images)) or 'none'}"
    synced_healthy = sync.get("status") == "Synced" and health.get("status") == "Healthy"
    revision_ready = revision in _observed_revisions(payload)
    # Empty expected_images must not pass a Healthy app at another Git SHA.
    # 未提供期望镜像时，不能把「别的 SHA 上 Healthy」当成成功。
    images_ready = bool(expected_images) and all(image in observed_images for image in expected_images)
    ready = synced_healthy and (revision_ready or images_ready)
    return ready, state


def wait_targets(services: tuple[Service, ...] | list[Service], overrides: dict[str, dict[str, str]] | None = None) -> dict[str, tuple[str, ...]]:
    """Map pipeline services and image overrides to Argo applications.

    将流水线服务与镜像覆盖映射为 Argo Application 及期望镜像。
    """
    images_by_app: dict[str, tuple[str, ...]] = {}
    for service in services:
        application = f"{service.kustomize_name}-dev"
        override = (overrides or {}).get(service.kustomize_name) or {}
        repository = override.get("newName") or service.image_repository
        digest = override.get("digest", "")
        tag = override.get("newTag", "")
        if digest:
            images_by_app[application] = (f"{repository}@{digest}",)
        elif tag:
            images_by_app[application] = (f"{repository}:{tag}",)
        else:
            images_by_app[application] = ()
    return images_by_app


def _request_hard_refresh(kubeconfig: str, application: str) -> None:
    # Ask the application controller to compare against current Git HEAD even when live
    # resources already match after ignoreDifferences.
    # 强制用当前 Git HEAD 做对比；ignoreDifferences 导致无 diff 时也能刷新 revision。
    result = subprocess.run(
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
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise ArgoError(f"failed to hard-refresh Argo application {application}: {detail}")


def _get_application(kubeconfig: str, application: str) -> tuple[dict | None, str]:
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
    if result.returncode != 0:
        return None, result.stderr.strip() or f"exit {result.returncode}"
    try:
        return json.loads(result.stdout), "unknown"
    except json.JSONDecodeError as exc:
        raise ArgoError(f"kubectl returned invalid Argo application JSON for {application}") from exc


def wait_applications(
    server: str,
    applications: tuple[str, ...] | list[str],
    revision: str,
    timeout: int = 600,
    refresh_interval: int = REFRESH_INTERVAL_SECONDS,
    expected_images: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict]:
    names = tuple(application for application in applications if application)
    if not names:
        raise ArgoError("at least one Argo application is required")
    images_by_app = expected_images or {}
    kubeconfig = os.environ.get("KUBECONFIG", "")
    pending = set(names)
    payloads: dict[str, dict] = {}
    last_states = {name: "unknown" for name in names}
    if kubeconfig:
        deadline = time.monotonic() + timeout
        last_refresh = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_refresh >= max(0, refresh_interval):
                for application in names:
                    if application in pending:
                        _request_hard_refresh(kubeconfig, application)
                last_refresh = now
            for application in names:
                if application not in pending:
                    continue
                payload, last_state = _get_application(kubeconfig, application)
                if payload is None:
                    last_states[application] = last_state
                    continue
                ready, last_states[application] = _ready_state(
                    payload,
                    revision,
                    expected_images=images_by_app.get(application, ()),
                )
                if ready:
                    payloads[application] = payload
                    pending.discard(application)
            if not pending:
                return payloads
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        remaining = ", ".join(f"{name}: {last_states[name]}" for name in names if name in pending)
        raise ArgoError(f"Argo application did not become healthy at revision {revision}: {remaining}")
    token = os.environ.get("ARGOCD_AUTH_TOKEN", "")
    endpoint = os.environ.get("ARGOCD_SERVER", server).rstrip("/")
    deadline = time.monotonic() + timeout
    last_refresh = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        refresh = now - last_refresh >= max(0, refresh_interval)
        if refresh:
            last_refresh = now
        for application in names:
            if application not in pending:
                continue
            url = f"https://{endpoint}/api/v1/applications/{application}"
            if refresh:
                url += "?refresh=hard"
            request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urlopen(request, timeout=min(20, max(1, int(deadline - time.monotonic())))) as response:
                    payload = json.loads(response.read())
            except (HTTPError, URLError, json.JSONDecodeError) as exc:
                last_states[application] = str(exc)
                continue
            ready, last_states[application] = _ready_state(
                payload,
                revision,
                expected_images=images_by_app.get(application, ()),
            )
            if ready:
                payloads[application] = payload
                pending.discard(application)
        if not pending:
            return payloads
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    remaining = ", ".join(f"{name}: {last_states[name]}" for name in names if name in pending)
    raise ArgoError(f"Argo application did not become healthy at revision {revision}: {remaining}")


def wait_application(
    server: str,
    application: str,
    revision: str,
    timeout: int = 600,
    refresh_interval: int = REFRESH_INTERVAL_SECONDS,
    expected_images: tuple[str, ...] = (),
) -> dict:
    payloads = wait_applications(
        server,
        (application,),
        revision,
        timeout=timeout,
        refresh_interval=refresh_interval,
        expected_images={application: expected_images} if expected_images else None,
    )
    return payloads[application]
