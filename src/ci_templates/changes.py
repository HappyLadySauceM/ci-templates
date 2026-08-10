from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Pipeline


MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_PATHS = 200
_SENSITIVE_PATH = re.compile(r"(^|/)(?:\.env(?:\..*)?|.*(?:secret|credential|kubeconfig|private[-_]?key).*)(?:$|/)", re.IGNORECASE)
_SENSITIVE_LINE = re.compile(
    r"(?:password|passwd|token|secret|private[_-]?key|authorization|cookie|api[_-]?key|dsn|database[_-]?url|redis[_-]?url|nats[_-]?url|credential(?:s)?)\s*[:=]|://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)


def changed_paths(base: str, head: str = "HEAD", cwd: str = ".") -> list[str]:
    command = ["git", "ls-tree", "-r", "--name-only", head] if base and set(base) == {"0"} else ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...{head}"]
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def resolve_revision(ref: str, cwd: str = ".") -> str:
    if ref and set(ref) == {"0"}:
        return ref
    result = subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _diff(base: str, head: str, paths: Iterable[str], cwd: str = ".") -> str:
    paths = tuple(paths)
    if not paths:
        return ""
    if base and set(base) == {"0"}:
        command = ["git", "show", "--format=", "--no-ext-diff", "--unified=20", head, "--", *paths]
    else:
        command = ["git", "diff", "--no-ext-diff", "--unified=20", f"{base}...{head}", "--", *paths]
    result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout


def _redact_diff(path: str, value: str) -> str:
    if _SENSITIVE_PATH.search(path):
        return "[REDACTED SENSITIVE FILE]"
    lines = []
    for line in value.splitlines():
        if _SENSITIVE_LINE.search(line):
            prefix = line[:1] if line[:1] in {"+", "-", " "} else " "
            lines.append(f"{prefix} [REDACTED SENSITIVE VALUE]")
        else:
            lines.append(line)
    return "\n".join(lines)


def _truncate(value: str, limit: int = MAX_CONTEXT_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n[DIFF TRUNCATED]"


def _service_paths(pipeline: Pipeline, service_name: str, paths: Iterable[str]) -> list[str]:
    service = next(item for item in pipeline.services if item.name == service_name)
    service_prefixes = (service.source_path.rstrip("/") + "/", service.deploy_snapshot.rstrip("/") + "/")
    shared = tuple(path.rstrip("/") for path in pipeline.shared_paths)
    return [
        path for path in paths
        if path in {service.source_path, service.dockerfile}
        or path.startswith(service_prefixes)
        or any(path == prefix or path.startswith(prefix + "/") for prefix in shared)
    ]


def build_release_context(pipeline: Pipeline, base: str, head: str, paths: Iterable[str], cwd: str = ".") -> dict[str, Any]:
    paths = tuple(paths)
    selected = affected_services(pipeline, paths)
    services: dict[str, dict[str, Any]] = {}
    for service_name in selected:
        service_paths = _service_paths(pipeline, service_name, paths)[:MAX_CONTEXT_PATHS]
        diff = "\n".join(_redact_diff(path, _diff(base, head, [path], cwd)) for path in service_paths)
        services[service_name] = {
            "paths": service_paths,
            "diff": _truncate(diff),
        }
    return {"base": base, "head": head, "paths": list(paths), "services": services}


def write_release_context(path: str | Path, context: dict[str, Any]) -> None:
    destination = Path(path)
    destination.write_text(json.dumps(context, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    destination.chmod(0o600)


def read_release_context(path: str | Path) -> dict[str, Any]:
    try:
        context = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read release change context {path}: {exc}") from exc
    if not isinstance(context, dict) or not isinstance(context.get("services"), dict):
        raise ValueError("release change context must contain a services object")
    return context


def affected_services(pipeline: Pipeline, paths: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    if not paths:
        return ()
    affected: set[str] = set()
    for service in pipeline.services:
        prefixes = (service.source_path.rstrip("/") + "/", service.deploy_snapshot.rstrip("/") + "/")
        if any(path in {service.source_path, service.dockerfile} or path.startswith(prefixes) for path in paths):
            affected.add(service.name)
    shared = tuple(path.rstrip("/") for path in pipeline.shared_paths)
    if any(path == prefix or path.startswith(prefix + "/") for path in paths for prefix in shared):
        affected.update(service.name for service in pipeline.services)
    return tuple(service.name for service in pipeline.services if service.name in affected)
