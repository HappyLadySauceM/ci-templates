from __future__ import annotations

import re
import subprocess
from pathlib import Path


VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)(?:\.(?P<patch>0|[1-9]\d*))?$")


def read_version(path: str | Path) -> tuple[int, int, int]:
    value = Path(path).read_text(encoding="utf-8").strip()
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"version must be MAJOR.MINOR[.PATCH], got {value!r}")
    return tuple(int(match.group(name) or 0) for name in ("major", "minor", "patch"))  # type: ignore[return-value]


def service_tag(service: str, version: tuple[int, int, int]) -> str:
    return f"{service}-v{version[0]}.{version[1]}.{version[2]}"


def aggregate_tag(version: tuple[int, int, int]) -> str:
    return f"v{version[0]}.{version[1]}.{version[2]}"


def next_patch(service: str, version: tuple[int, int, int], cwd: str = ".") -> tuple[int, int, int]:
    prefix = f"{service}-v{version[0]}.{version[1]}."
    result = subprocess.run(["git", "tag", "--list", f"{prefix}*"], cwd=cwd, check=True, capture_output=True, text=True)
    patches = []
    for tag in result.stdout.splitlines():
        suffix = tag.removeprefix(prefix)
        if suffix.isdigit():
            patches.append(int(suffix))
    return version[0], version[1], max(patches, default=0) + 1


def release_tag(service: str, version: tuple[int, int, int], cwd: str = ".") -> str:
    prefix = f"{service}-v{version[0]}.{version[1]}."
    result = subprocess.run(
        ["git", "tag", "--points-at", "HEAD", "--list", f"{prefix}*"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    patches = [int(tag.removeprefix(prefix)) for tag in result.stdout.splitlines() if tag.removeprefix(prefix).isdigit()]
    if patches:
        return service_tag(service, (version[0], version[1], max(patches)))
    return service_tag(service, next_patch(service, version, cwd=cwd))


def _list_tags(cwd: str, pattern: str, *, points_at_head: bool = False) -> list[str]:
    command = ["git", "tag"]
    if points_at_head:
        command.extend(["--points-at", "HEAD"])
    command.extend(["--list", pattern])
    result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return [line for line in result.stdout.splitlines() if line]


def fetch_release_tags(cwd: str = ".") -> None:
    """Fetch tags so a shallow checkout does not invent v0.1.1.

    拉取远端 tag，避免浅克隆把下一个聚合版本算成 v0.1.1。
    """

    subprocess.run(
        ["git", "fetch", "--tags", "--force"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _patch_numbers(tag_prefix: str, tags: list[str]) -> list[int]:
    patches = []
    for tag in tags:
        suffix = tag.removeprefix(tag_prefix)
        if suffix.isdigit():
            patches.append(int(suffix))
    return patches


def _legacy_tag_prefix(prefix: str, version: tuple[int, int, int]) -> str:
    return f"{prefix}-v{version[0]}.{version[1]}."


def aggregate_release_tag(prefix: str, version: tuple[int, int, int], cwd: str = ".") -> str:
    # New aggregate tags are vMAJOR.MINOR.PATCH. Prefixed tags still count
    # when retrying the same commit or choosing the next patch.
    # 新的聚合 tag 使用纯版本号 vMAJOR.MINOR.PATCH。同 commit 重试或选择下一个
    # patch 时，仍计入已有的 prefix tag。
    version_prefix = f"v{version[0]}.{version[1]}."
    current = _patch_numbers(version_prefix, _list_tags(cwd, f"{version_prefix}*", points_at_head=True))
    if current:
        return aggregate_tag((version[0], version[1], max(current)))
    if prefix:
        legacy_prefix = _legacy_tag_prefix(prefix, version)
        legacy = _patch_numbers(legacy_prefix, _list_tags(cwd, f"{legacy_prefix}*", points_at_head=True))
        if legacy:
            return f"{prefix}-v{version[0]}.{version[1]}.{max(legacy)}"
    existing = _patch_numbers(version_prefix, _list_tags(cwd, f"{version_prefix}*"))
    if prefix:
        legacy_prefix = _legacy_tag_prefix(prefix, version)
        existing.extend(_patch_numbers(legacy_prefix, _list_tags(cwd, f"{legacy_prefix}*")))
    return aggregate_tag((version[0], version[1], max(existing, default=0) + 1))
