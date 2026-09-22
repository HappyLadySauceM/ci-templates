"""Content-addressed, node-local fallback for GitHub Actions artifacts."""

from __future__ import annotations

import hashlib
import json
import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from zipfile import BadZipFile, ZipFile

from .github import GitHubError, _request
from .transport import request_with_retry


class ArtifactCacheError(RuntimeError):
    pass


_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\Z")


class _NoAutomaticRedirect(HTTPRedirectHandler):
    """Keep GitHub credentials on the API request, never on its signed URL."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


_GITHUB_API_OPENER = build_opener(_NoAutomaticRedirect())


def _open_github_artifact_redirect(request: Request, *, timeout: float) -> object:
    """Return GitHub's expected 302 as a response instead of forwarding auth."""

    try:
        return _GITHUB_API_OPENER.open(request, timeout=timeout)
    except HTTPError as response:
        if response.code == 302:
            return response
        raise


def _metadata() -> tuple[str, str, str]:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if not repository or not run_id or not sha:
        raise ArtifactCacheError("GITHUB_REPOSITORY, GITHUB_RUN_ID and GITHUB_SHA are required")
    return repository, run_id, sha


def _validate_name(name: str) -> str:
    value = name.strip()
    if not _SAFE_NAME.fullmatch(value):
        raise ArtifactCacheError(f"unsafe artifact name: {name!r}")
    return value


def _root() -> Path:
    root = Path(os.environ.get("CI_ARTIFACT_CACHE_ROOT", "/cache/ci-templates/artifacts"))
    if not root.is_absolute() or root == Path("/"):
        raise ArtifactCacheError("CI_ARTIFACT_CACHE_ROOT must be an absolute non-root path")
    return root


def _entry(name: str) -> Path:
    _, run_id, sha = _metadata()
    return _root() / run_id / sha / _validate_name(name)


def _cache_zip(name: str) -> tuple[Path, Path]:
    entry = _entry(name)
    return entry / "payload.zip", entry / "metadata.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_cached(name: str) -> tuple[Path, dict] | None:
    payload, metadata = _cache_zip(name)
    try:
        info = json.loads(metadata.read_text(encoding="utf-8"))
        if not payload.is_file() or info.get("sha256") != _sha256(payload):
            _discard_entry(name)
            return None
        repository, run_id, sha = _metadata()
        if info.get("repository") != repository or info.get("run_id") != run_id or info.get("sha") != sha:
            return None
        return payload, info
    except (OSError, ValueError, KeyError):
        _discard_entry(name)
        return None


def _discard_entry(name: str) -> None:
    """Drop a corrupt entry before a remote retry; never use stale bytes."""

    try:
        shutil.rmtree(_entry(name))
    except FileNotFoundError:
        pass


def _secure_extract(payload: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    temp_root = os.environ.get("RUNNER_TEMP", "").strip()
    if temp_root:
        Path(temp_root).mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="artifact-extract-", dir=temp_root or None))
    try:
        with ZipFile(payload) as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                mode = (member.external_attr >> 16) & 0o170000
                if relative.is_absolute() or ".." in relative.parts or mode == stat.S_IFLNK:
                    raise ArtifactCacheError("artifact contains an unsafe path")
            archive.extractall(stage)
        for item in stage.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
    except BadZipFile as exc:
        raise ArtifactCacheError("cached artifact is not a valid zip") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _artifact_listing() -> list[dict]:
    repository, run_id, _ = _metadata()
    items: list[dict] = []
    for page in range(1, 101):
        payload = _request("GET", f"/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}") or {}
        page_items = payload.get("artifacts", [])
        if not isinstance(page_items, list):
            raise ArtifactCacheError("GitHub artifact listing is invalid")
        items.extend(item for item in page_items if isinstance(item, dict) and not item.get("expired"))
        if len(page_items) < 100:
            break
    else:
        raise ArtifactCacheError("GitHub artifact listing exceeded pagination limit")
    return items


def _download_from_github(name: str, *, selected: dict | None = None) -> tuple[Path, dict]:
    repository, run_id, sha = _metadata()
    matches = [item for item in _artifact_listing() if item.get("name") == name]
    if not matches:
        raise ArtifactCacheError(f"GitHub artifact not found: {name}")
    selected = selected or max(matches, key=lambda item: int(item.get("id") or 0))
    archive_url = str(selected.get("archive_download_url") or "")
    artifact_id = str(selected.get("id") or "")
    if not archive_url or not artifact_id:
        raise ArtifactCacheError(f"GitHub artifact has no download URL: {name}")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ArtifactCacheError("GITHUB_TOKEN is required to download artifacts")
    request = Request(
        archive_url,
        # The archive_download_url is a GitHub REST endpoint which returns a
        # short-lived 302 to the ZIP. GitHub negotiates that redirect using
        # its JSON media type; application/zip is not accepted on this API
        # request and yields HTTP 415 before the archive host is reached.
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
        method="GET",
    )
    redirect_context = request_with_retry(_open_github_artifact_redirect, request, timeout=30)
    with redirect_context as redirect_response:
        if redirect_response.getcode() != 302:
            raise ArtifactCacheError("GitHub artifact download did not return a redirect")
        location = redirect_response.headers.get("Location")
    if not location:
        raise ArtifactCacheError("GitHub artifact redirect is missing its download URL")
    download_url = urljoin(archive_url, location)
    parsed_download_url = urlsplit(download_url)
    if parsed_download_url.scheme != "https" or not parsed_download_url.netloc:
        raise ArtifactCacheError("GitHub artifact redirect URL must use HTTPS")
    # The Location URL is a short-lived signed URL. Fetch it without the
    # GitHub bearer token; forwarding that token can make the storage service
    # reject the signed request and would disclose credentials cross-origin.
    archive_request = Request(download_url, headers={"Accept": "application/zip"}, method="GET")
    entry = _entry(name)
    entry.mkdir(parents=True, exist_ok=True)
    temporary = entry.parent / f".{entry.name}.zip.tmp"
    try:
        response_context = request_with_retry(urlopen, archive_request, timeout=120)
        with response_context as response, temporary.open("wb") as stream:
            # ``HTTPResponse`` implements sized reads; a few lightweight
            # clients expose only ``read()``. Keep the streaming path for
            # real downloads while remaining compatible with both forms.
            try:
                shutil.copyfileobj(response, stream)
            except TypeError:
                stream.write(response.read())
        digest = _sha256(temporary)
        info = {"repository": repository, "run_id": run_id, "sha": sha, "name": name, "artifact_id": artifact_id, "sha256": digest}
        temporary.replace(entry / "payload.zip")
        metadata_temporary = entry.parent / f".{entry.name}.metadata.tmp"
        metadata_temporary.write_text(json.dumps(info, sort_keys=True) + "\n", encoding="utf-8")
        metadata_temporary.replace(entry / "metadata.json")
        return entry / "payload.zip", info
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def restore(name: str, destination: str) -> dict[str, object]:
    name = _validate_name(name)
    cached = _read_cached(name)
    source = "cache"
    if cached is None:
        source = "github"
        try:
            cached = _download_from_github(name)
        except (ArtifactCacheError, GitHubError):
            raise
    payload, info = cached
    try:
        _secure_extract(payload, Path(destination))
    except ArtifactCacheError:
        # A hash-valid but structurally invalid archive is still unusable.
        # Remove it so the next attempt fetches a fresh remote copy instead of
        # repeatedly replaying the same corrupt cache entry.
        _discard_entry(name)
        raise
    return {"name": name, "source": source, "sha256": info["sha256"], "artifact_id": info.get("artifact_id", "")}


def restore_pattern(pattern: str, destination: str) -> list[dict[str, object]]:
    pattern = pattern.strip()
    if not pattern or any(part in {"", ".", ".."} for part in pattern.split("/")):
        raise ArtifactCacheError("artifact pattern must be non-empty and relative")
    cached_names: dict[str, dict] = {}
    cache_dir = _root() / _metadata()[1] / _metadata()[2]
    if cache_dir.is_dir():
        for entry in cache_dir.iterdir():
            if entry.is_dir() and fnmatch.fnmatch(entry.name, pattern):
                cached = _read_cached(entry.name)
                if cached is not None:
                    cached_names[entry.name] = cached[1]
    try:
        remote_matches = [
            item for item in _artifact_listing()
            if fnmatch.fnmatch(str(item.get("name") or ""), pattern)
        ]
    except (ArtifactCacheError, GitHubError):
        # A complete, digest-validated local set is a safe offline fallback.
        # Do not silently use it when the cache is incomplete.
        if not cached_names:
            raise
        remote_matches = []
    matches_by_name = {str(item.get("name") or ""): item for item in remote_matches}
    matches_by_name.update({name: {"name": name, "id": info.get("artifact_id", "")} for name, info in cached_names.items()})
    if not matches_by_name:
        raise ArtifactCacheError(f"GitHub artifacts do not match pattern: {pattern}")
    results: list[dict[str, object]] = []
    for item in sorted(matches_by_name.values(), key=lambda value: str(value.get("name") or "")):
        name = _validate_name(str(item.get("name") or ""))
        cached = _read_cached(name)
        source = "cache"
        if cached is None:
            source = "github"
            cached = _download_from_github(name, selected=item)
        payload, info = cached
        try:
            _secure_extract(payload, Path(destination))
        except ArtifactCacheError:
            _discard_entry(name)
            raise
        results.append({"name": name, "source": source, "sha256": info["sha256"], "artifact_id": info.get("artifact_id", "")})
    return results


def cache_prune(*, max_age_seconds: int = 72 * 3600, dry_run: bool = False) -> list[str]:
    """Remove only validated artifact-cache entries older than the retention."""

    root = _root()
    if not root.exists():
        return []
    cutoff = time.time() - max_age_seconds
    removed: list[str] = []
    for path in root.glob("*/*/*"):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        removed.append(str(path))
        if not dry_run:
            shutil.rmtree(path)
    return removed
