from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Pipeline, Service


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


def _shared_prefixes(pipeline: Pipeline) -> tuple[str, ...]:
    return tuple(path.rstrip("/") for path in pipeline.shared_paths)


def _is_under_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_configured_shared_path(pipeline: Pipeline, path: str) -> bool:
    return any(_is_under_prefix(path, prefix) for prefix in _shared_prefixes(pipeline))


def _service_business_match(service: Service, path: str) -> bool:
    source = service.source_path.rstrip("/")
    deploy = service.deploy_snapshot.rstrip("/")
    return path in {service.source_path, service.deploy_snapshot} or _is_under_prefix(path, source) or _is_under_prefix(path, deploy)


def _service_source_match(service: Service, path: str) -> bool:
    source = service.source_path.rstrip("/")
    return path in {service.source_path, service.dockerfile} or _is_under_prefix(path, source)


def _service_deploy_match(service: Service, path: str) -> bool:
    deploy = service.deploy_snapshot.rstrip("/")
    return path == deploy or _is_under_prefix(path, deploy)


def _bucket_diff(base: str, head: str, paths: list[str], cwd: str) -> dict[str, Any]:
    limited = paths[:MAX_CONTEXT_PATHS]
    diff = "\n".join(_redact_diff(path, _diff(base, head, [path], cwd)) for path in limited)
    return {"paths": limited, "diff": _truncate(diff)}


def classify_release_paths(pipeline: Pipeline, paths: Iterable[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Split changed paths into shared/CI versus service-business buckets.

    将变更路径划分为共享/CI 桶与服务业务桶。
    Dockerfile-only rebuilds without services/<svc> or deploy/<svc> changes stay shared.
    仅 Dockerfile、无业务代码变更时归入共享桶。
    """
    shared: list[str] = []
    exclusive: dict[str, list[str]] = {service.name: [] for service in pipeline.services}
    for path in paths:
        if _is_configured_shared_path(pipeline, path):
            shared.append(path)
            continue
        business_owner: str | None = None
        for service in pipeline.services:
            if _service_business_match(service, path):
                business_owner = service.name
                break
        if business_owner is not None:
            exclusive[business_owner].append(path)
            continue
        # Dockerfile-only and other non-business paths are shared release notes.
        # 仅 Dockerfile 及其他非业务路径写入共享发布说明。
        shared.append(path)
    return shared, {name: values for name, values in exclusive.items() if values}


def build_release_context(pipeline: Pipeline, base: str, head: str, paths: Iterable[str], cwd: str = ".") -> dict[str, Any]:
    paths = list(paths)
    shared_paths, exclusive = classify_release_paths(pipeline, paths)
    services = {
        name: _bucket_diff(base, head, service_paths, cwd)
        for name, service_paths in exclusive.items()
    }
    return {
        "base": base,
        "head": head,
        "paths": paths,
        "shared": _bucket_diff(base, head, shared_paths, cwd),
        "services": services,
    }


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
    shared = context.get("shared")
    if shared is None:
        context["shared"] = {"paths": [], "diff": ""}
    elif not isinstance(shared, dict):
        raise ValueError("release change context shared bucket must be an object")
    return context


def affected_services(pipeline: Pipeline, paths: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    if not paths:
        return ()
    affected: set[str] = set()
    for service in pipeline.services:
        if any(_service_source_match(service, path) for path in paths):
            affected.add(service.name)
    shared = _shared_prefixes(pipeline)
    if any(_is_under_prefix(path, prefix) for path in paths for prefix in shared):
        affected.update(service.name for service in pipeline.services)
    return tuple(service.name for service in pipeline.services if service.name in affected)


def deploy_services(pipeline: Pipeline, paths: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    return tuple(
        service.name
        for service in pipeline.services
        if any(_service_deploy_match(service, path) for path in paths)
    )


def deploy_changed(pipeline: Pipeline, paths: Iterable[str]) -> bool:
    root = pipeline.deploy_root.rstrip("/")
    return any(_is_under_prefix(path, root) for path in paths)


def release_services(pipeline: Pipeline, paths: Iterable[str]) -> tuple[str, ...]:
    build = set(affected_services(pipeline, paths))
    deploy = set(deploy_services(pipeline, paths))
    return tuple(service.name for service in pipeline.services if service.name in build | deploy)
