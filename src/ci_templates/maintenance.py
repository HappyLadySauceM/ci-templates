"""Fail-closed maintenance operations for CI artifacts and Harbor candidates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from typing import Callable

from .config import Pipeline
from .github import GitHubError, _request
from .harbor import HarborClient, ImageRef


class MaintenanceError(RuntimeError):
    pass


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _active_commit_shas(repository: str) -> set[str]:
    commits: set[str] = set()
    for status in ("queued", "in_progress"):
        payload = _request(
            "GET",
            f"/repos/{repository}/actions/runs?branch=dev&status={status}&per_page=100",
        ) or {}
        runs = payload.get("workflow_runs", [])
        if not isinstance(runs, list):
            raise MaintenanceError("GitHub active-run response is invalid")
        for run in runs:
            sha = str(run.get("head_sha") or "")
            if sha:
                commits.add(sha)
    return commits


def prune_candidates(
    config: Pipeline,
    *,
    max_age_hours: int = 72,
    protected_digests: set[str] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    harbor_factory: Callable[[str], HarborClient] = HarborClient,
) -> list[dict[str, object]]:
    if max_age_hours < 1:
        raise MaintenanceError("max_age_hours must be positive")
    now = now or datetime.now(timezone.utc)
    protected = set(protected_digests or set())
    configured_protected = os.environ.get("CI_GITOPS_PROTECTED_DIGESTS_JSON", "").strip()
    if configured_protected:
        try:
            values = json.loads(configured_protected)
        except json.JSONDecodeError as exc:
            raise MaintenanceError("CI_GITOPS_PROTECTED_DIGESTS_JSON is invalid; refusing candidate deletion") from exc
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise MaintenanceError("CI_GITOPS_PROTECTED_DIGESTS_JSON must be a list; refusing candidate deletion")
        protected.update(item for item in values if item)
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository:
        raise MaintenanceError("GITHUB_REPOSITORY is required for candidate pruning")
    try:
        active_shas = _active_commit_shas(repository)
    except (GitHubError, OSError, ValueError) as exc:
        raise MaintenanceError(f"cannot verify active GitHub runs; refusing candidate deletion: {exc}") from exc
    harbor = harbor_factory(config.harbor_registry)
    for service in config.services:
        for stable_tag in (config.active_image_tag, config.previous_image_tag):
            if service.image_repository.startswith(config.harbor_registry + "/"):
                image = ImageRef.parse(f"{service.image_repository}:{stable_tag}")
            else:
                image = ImageRef(config.harbor_registry, service.image_repository, stable_tag)
            digest = harbor.manifest_digest(image)
            if digest:
                protected.add(digest)
    cutoff = now - timedelta(hours=max_age_hours)
    actions: list[dict[str, object]] = []
    for item in harbor.list_candidate_tags(config.harbor_project):
        tag = str(item.get("tag") or "")
        digest = str(item.get("digest") or "")
        pushed = _parse_time(str(item.get("push_time") or ""))
        if not pushed or pushed > cutoff:
            continue
        sha = tag.removeprefix("sha-")
        if sha in active_shas or digest in protected:
            continue
        image = ImageRef(config.harbor_registry, f"{config.harbor_project}/{item['repository']}", tag)
        action: dict[str, object] = {"image": image.tag_ref, "digest": digest, "dry_run": dry_run}
        if not dry_run:
            harbor.delete_tag(image)
            action["deleted"] = True
        else:
            action["deleted"] = False
        actions.append(action)
    return actions
