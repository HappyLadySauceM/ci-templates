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


def next_patch(service: str, version: tuple[int, int, int], cwd: str = ".") -> tuple[int, int, int]:
    prefix = f"{service}-v{version[0]}.{version[1]}."
    result = subprocess.run(["git", "tag", "--list", f"{prefix}*"], cwd=cwd, check=True, capture_output=True, text=True)
    patches = []
    for tag in result.stdout.splitlines():
        suffix = tag.removeprefix(prefix)
        if suffix.isdigit():
            patches.append(int(suffix))
    return version[0], version[1], max(patches, default=0) + 1
