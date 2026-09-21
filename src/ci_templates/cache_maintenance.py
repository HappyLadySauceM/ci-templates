"""Safe cleanup of the explicit node-local CI cache directories."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import time


class CacheMaintenanceError(RuntimeError):
    pass


_CACHE_DIRS = {
    "artifacts": 72 * 3600,
    "cargo": 30 * 24 * 3600,
    "go": 30 * 24 * 3600,
    "npm": 30 * 24 * 3600,
    "pnpm": 30 * 24 * 3600,
    "playwright": 30 * 24 * 3600,
    "actions-tools": 30 * 24 * 3600,
}


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".ci-templates-cache.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CacheMaintenanceError("another cache maintenance process is active") from exc
        yield
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prune_cache(
    root: str = "/cache",
    *,
    dry_run: bool = False,
    high_watermark: float = 0.80,
    low_watermark: float = 0.70,
    now: float | None = None,
) -> list[str]:
    cache_root = Path(root)
    if not cache_root.is_absolute() or cache_root in {Path("/"), Path("/home"), Path("/tmp")}:
        raise CacheMaintenanceError("cache root must be an explicit absolute cache directory")
    if not 0 < low_watermark < high_watermark < 1:
        raise CacheMaintenanceError("invalid cache watermarks")
    if now is None:
        now = time.time()
    removed: list[str] = []
    with _lock(cache_root):
        usage = 1.0 - (os.statvfs(cache_root).f_bavail / max(1, os.statvfs(cache_root).f_blocks))
        for name, max_age in _CACHE_DIRS.items():
            directory = cache_root / name
            if not directory.is_dir():
                continue
            # Run retention cleanup for ephemeral run artifacts every time. For
            # package/tool caches wait for pressure so a warm cache remains useful.
            if name != "artifacts" and usage < high_watermark:
                continue
            cutoff = now - max_age
            candidates = []
            for path in directory.rglob("*"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path.is_file() and stat.st_mtime < cutoff:
                    candidates.append((stat.st_mtime, path))
            for _, path in sorted(candidates):
                relative = str(path.relative_to(cache_root))
                removed.append(relative)
                if not dry_run:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            if not dry_run:
                for path in sorted(directory.rglob("*"), reverse=True):
                    if path.is_dir():
                        try:
                            path.rmdir()
                        except OSError:
                            pass
            if not dry_run:
                usage = 1.0 - (os.statvfs(cache_root).f_bavail / max(1, os.statvfs(cache_root).f_blocks))
                if usage <= low_watermark:
                    break
    return removed
