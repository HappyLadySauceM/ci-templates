from __future__ import annotations

import subprocess
import json
import time


def run(command: tuple[str, ...], cwd: str = ".") -> None:
    if not command:
        raise ValueError("smoke command must not be empty")
    subprocess.run(list(command), cwd=cwd, check=True)


def run_kubernetes(namespace: str = "knowledge-core-dev", kubeconfig: str | None = None, attempts: int = 30) -> None:
    """Exercise every admin readiness endpoint and the public document read path."""
    endpoints = (
        ("knowledge-core-gateway", "8082", "/readyz"),
        ("knowledge-core-identity", "8081", "/readyz"),
        ("knowledge-core-knowledge", "8083", "/readyz"),
        ("knowledge-core-collaboration", "8084", "/health/ready"),
        ("knowledge-core-gateway", "8080", "/health/ready"),
        ("knowledge-core-gateway", "8080", "/api/v1/documents?limit=1"),
    )
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
