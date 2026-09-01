from __future__ import annotations

import subprocess
import json
import time
import os


def run(command: tuple[str, ...], cwd: str = ".", env: dict[str, str] | None = None) -> None:
    if not command:
        raise ValueError("smoke command must not be empty")
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    subprocess.run(list(command), cwd=cwd, check=True, env=process_env)


def run_kubernetes(
    namespace: str,
    kubeconfig: str | None = None,
    attempts: int = 30,
    endpoints: tuple[tuple[str, str, str], ...] = (),
) -> None:
    """Exercise configured readiness and smoke endpoints through the API proxy."""
    if not namespace:
        raise ValueError("smoke namespace must not be empty")
    if not endpoints:
        raise ValueError("at least one Kubernetes smoke endpoint is required")
    for service, port, path in endpoints:
        raw_path = f"/api/v1/namespaces/{namespace}/services/http:{service}:{port}/proxy{path}"
        command = ["kubectl"]
        if kubeconfig:
            command.extend(["--kubeconfig", kubeconfig])
        command.extend(["get", "--raw", raw_path])
        last_error = ""
        for _ in range(attempts):
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            if result.returncode == 0:
                try:
                    json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"smoke endpoint returned invalid JSON: {service}:{port}{path}") from exc
                break
            last_error = result.stderr.strip()
            time.sleep(2)
        else:
            raise RuntimeError(f"smoke endpoint failed: {service}:{port}{path}: {last_error}")
