from __future__ import annotations

import json
import os
import time
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ArgoError(RuntimeError):
    pass


def wait_application(server: str, application: str, revision: str, timeout: int = 600) -> dict:
    kubeconfig = os.environ.get("KUBECONFIG", "")
    if kubeconfig:
        deadline = time.monotonic() + timeout
        last_state = "unknown"
        while time.monotonic() < deadline:
            result = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, "get", "application", application, "-n", "argocd", "-o", "json"], check=False, capture_output=True, text=True)
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise ArgoError("kubectl returned invalid Argo application JSON") from exc
                status = payload.get("status", {})
                sync = status.get("sync", {})
                health = status.get("health", {})
                if sync.get("revision") == revision and sync.get("status") == "Synced" and health.get("status") == "Healthy":
                    return payload
                last_state = f"revision={sync.get('revision')} sync={sync.get('status')} health={health.get('status')}"
            else:
                last_state = result.stderr.strip()
            time.sleep(5)
        raise ArgoError(f"Argo application did not become healthy at revision {revision}: {last_state}")
    token = os.environ.get("ARGOCD_AUTH_TOKEN", "")
    endpoint = os.environ.get("ARGOCD_SERVER", server).rstrip("/")
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        request = Request(f"https://{endpoint}/api/v1/applications/{application}", headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urlopen(request, timeout=min(20, max(1, int(deadline - time.monotonic())))) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            last_state = str(exc)
        else:
            status = payload.get("status", {})
            sync = status.get("sync", {})
            health = status.get("health", {})
            if sync.get("revision") == revision and sync.get("status") == "Synced" and health.get("status") == "Healthy":
                return payload
            last_state = f"revision={sync.get('revision')} sync={sync.get('status')} health={health.get('status')}"
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ArgoError(f"Argo application {application} did not become healthy at revision {revision}: {last_state}")
