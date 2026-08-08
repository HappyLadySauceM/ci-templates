from __future__ import annotations

import subprocess
from collections.abc import Iterable

from .config import Pipeline


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


def affected_services(pipeline: Pipeline, paths: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    if not paths:
        return ()
    affected: set[str] = set()
    for service in pipeline.services:
        prefixes = (service.source_path.rstrip("/") + "/", service.deploy_snapshot.rstrip("/") + "/")
        if any(path == service.source_path or path.startswith(prefixes) for path in paths):
            affected.add(service.name)
    shared = tuple(path.rstrip("/") for path in pipeline.shared_paths)
    if any(path == prefix or path.startswith(prefix + "/") for path in paths for prefix in shared):
        affected.update(service.name for service in pipeline.services)
    return tuple(service.name for service in pipeline.services if service.name in affected)
