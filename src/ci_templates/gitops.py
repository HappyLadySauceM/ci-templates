from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import base64
import os
import time
from typing import Any

import yaml


class GitOpsError(RuntimeError):
    pass


def _git(args: list[str], *, cwd: str | Path | None = None, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=check, env=env, stdout=sys.stderr, text=True)


def _configure_identity(worktree: Path) -> None:
    _git(["config", "user.name", "knowledge-core-ci"], cwd=worktree)
    _git(["config", "user.email", "knowledge-core-ci@noreply.local"], cwd=worktree)


def sync_snapshot(source_root: str | Path, snapshot_root: str | Path) -> None:
    source = Path(source_root).resolve()
    target = Path(snapshot_root).resolve()
    if source == target or source not in source.parents and target in source.parents:
        raise GitOpsError("source and snapshot paths overlap unsafely")
    if not source.is_dir():
        raise GitOpsError(f"deploy source does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def update_images(kustomization: str | Path, overrides: dict[str, dict[str, str]]) -> None:
    path = Path(kustomization)
    try:
        document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise GitOpsError(f"cannot read kustomization {path}: {exc}") from exc
    images = document.setdefault("images", [])
    if not isinstance(images, list):
        raise GitOpsError("kustomization images must be a list")
    by_name = {entry.get("name"): entry for entry in images if isinstance(entry, dict) and entry.get("name")}
    for name, override in overrides.items():
        entry = by_name.setdefault(name, {"name": name})
        for key in ("newName", "newTag", "digest"):
            if key in override:
                entry[key] = override[key]
        if "digest" in override:
            entry.pop("newTag", None)
        if entry not in images:
            images.append(entry)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=False), encoding="utf-8")


def promote_snapshot(source_deploy: str | Path, gitops_repo: str, gitops_path: str, kustomization: str, branch: str, source_sha: str, image_overrides: dict[str, dict[str, str]] | None = None) -> tuple[str, str]:
    """Copy deploy source into the GitOps repo and publish a fast-forward snapshot."""
    source = Path(source_deploy).resolve()
    if not source.is_dir():
        raise GitOpsError(f"deploy source does not exist: {source}")
    git_env = os.environ.copy()
    git_args: list[str] = []
    token = git_env.get("GITOPS_TOKEN", "")
    if token and gitops_repo.startswith("https://"):
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        git_args = ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]
    last_push_error: subprocess.CalledProcessError | None = None
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix="gitops-commit-") as temp:
            worktree = Path(temp) / "repo"
            subprocess.run(["git", *git_args, "clone", "--depth", "1", "--branch", branch, gitops_repo, str(worktree)], check=True, capture_output=True, text=True, env=git_env)
            base_revision = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
            target = worktree / gitops_path / "deploy"
            sync_snapshot(source, target)
            if image_overrides:
                update_images(worktree / gitops_path / kustomization, image_overrides)
            marker = worktree / gitops_path / ".source-revision"
            marker.write_text(source_sha.strip() + "\n", encoding="utf-8")
            _configure_identity(worktree)
            _git([
                "add",
                str(Path(gitops_path) / "deploy"),
                str(Path(gitops_path) / ".source-revision"),
                str(Path(gitops_path) / kustomization),
            ], cwd=worktree)
            changed = _git(["diff", "--cached", "--quiet"], cwd=worktree, check=False)
            if changed.returncode == 0:
                return base_revision, base_revision
            _git(["commit", "-m", f"chore({gitops_path}): sync deploy from {source_sha[:12]}"], cwd=worktree)
            try:
                _git([*git_args, "push", "origin", f"HEAD:{branch}"], cwd=worktree, env=git_env)
            except subprocess.CalledProcessError as exc:
                last_push_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
            return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip(), base_revision
    raise GitOpsError("GitOps branch moved during snapshot promotion after 3 attempts") from last_push_error


def rollback_snapshot(gitops_repo: str, branch: str, revision: str) -> str:
    """Revert one CI snapshot commit and push it as a normal fast-forward."""
    if not revision:
        raise GitOpsError("rollback revision is required")
    git_env = os.environ.copy()
    git_args: list[str] = []
    token = git_env.get("GITOPS_TOKEN", "")
    if token and gitops_repo.startswith("https://"):
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        git_args = ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]
    with tempfile.TemporaryDirectory(prefix="gitops-rollback-") as temp:
        worktree = Path(temp) / "repo"
        subprocess.run(["git", *git_args, "clone", "--depth", "2", "--branch", branch, gitops_repo, str(worktree)], check=True, capture_output=True, text=True, env=git_env)
        head = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
        if head != revision:
            raise GitOpsError("GitOps branch moved before rollback; refusing to overwrite it")
        _configure_identity(worktree)
        _git(["revert", "--no-edit", revision], cwd=worktree)
        _git([*git_args, "push", "origin", f"HEAD:{branch}"], cwd=worktree, env=git_env)
        return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True).strip()
