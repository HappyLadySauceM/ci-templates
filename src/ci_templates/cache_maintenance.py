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
    # Project-local same-node artifact caches used by older pipelines. Keep
    # them under the same retention and protection rules as the shared cache.
    "knowledge-core/artifacts": 72 * 3600,
    "knowledge-core-web/artifacts": 72 * 3600,
    "cargo": 30 * 24 * 3600,
    "go": 30 * 24 * 3600,
    "npm": 30 * 24 * 3600,
    "pnpm": 30 * 24 * 3600,
    "playwright": 30 * 24 * 3600,
    "actions-tools": 30 * 24 * 3600,
}

_ARTIFACT_CACHE_DIRS = {
    "artifacts",
    "knowledge-core/artifacts",
    "knowledge-core-web/artifacts",
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
    removed_paths: set[Path] = set()
    reclaimed_bytes = 0

    def disk_state() -> tuple[float, int, int]:
        stats = os.statvfs(cache_root)
        capacity = max(1, stats.f_blocks * stats.f_frsize)
        available = stats.f_bavail * stats.f_frsize
        usage = 1.0 - (available / capacity)
        return usage, available, capacity

    def projected_usage() -> float:
        usage, available, capacity = disk_state()
        return 1.0 - min(capacity, available + reclaimed_bytes) / capacity

    def mark_for_removal(path: Path) -> None:
        nonlocal reclaimed_bytes
        if path in removed_paths:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        removed_paths.add(path)
        reclaimed_bytes += size
        removed.append(str(path.relative_to(cache_root)))
        if not dry_run:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    with _lock(cache_root):
        for name, max_age in _CACHE_DIRS.items():
            directory = cache_root / name
            if not directory.is_dir():
                continue
            # Run retention cleanup for ephemeral run artifacts every time. For
            # package/tool caches wait for pressure so a warm cache remains useful.
            if name not in _ARTIFACT_CACHE_DIRS and projected_usage() < high_watermark:
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
                mark_for_removal(path)
            if not dry_run:
                for path in sorted(directory.rglob("*"), reverse=True):
                    if path.is_dir():
                        try:
                            path.rmdir()
                        except OSError:
                            pass

        # Retention cleanup is deliberately followed by an LRU pass under
        # pressure.  Fresh package/tool entries may still be evicted to bring
        # the node back to the low watermark; run artifacts remain protected
        # for their explicit 72-hour retention window.
        if projected_usage() > high_watermark:
            pressure_candidates: list[tuple[float, float, Path]] = []
            for name in _CACHE_DIRS:
                if name in _ARTIFACT_CACHE_DIRS:
                    continue
                directory = cache_root / name
                if not directory.is_dir():
                    continue
                for path in directory.rglob("*"):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if path.is_file() and path not in removed_paths:
                        pressure_candidates.append((stat.st_atime, stat.st_mtime, path))
            for _, _, path in sorted(pressure_candidates):
                mark_for_removal(path)
                if projected_usage() <= low_watermark:
                    break
    return removed
