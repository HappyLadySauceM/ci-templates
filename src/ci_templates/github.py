from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    pass


def _request(method: str, endpoint: str, body: object | None = None, *, not_found_ok: bool = False) -> dict | None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise GitHubError("GITHUB_TOKEN is required")
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        f"https://api.github.com{endpoint}", data=data,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
    except HTTPError as exc:
        if exc.code == 404 and not_found_ok:
            return None
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise GitHubError(f"GitHub API {method} {endpoint} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise GitHubError(f"GitHub API {method} {endpoint} failed: {exc}") from exc
    return json.loads(payload) if payload else {}


def create_release(repository: str, tag: str, target: str, body: str, *, name: str | None = None) -> dict:
    endpoint = f"/repos/{repository}/releases/tags/{quote(tag, safe='')}"
    existing = _request("GET", endpoint, not_found_ok=True)
    if existing is not None:
        return existing
    created = _request(
        "POST",
        f"/repos/{repository}/releases",
        {
            "tag_name": tag,
            "target_commitish": target,
            "name": name or tag,
            "body": body,
            "draft": False,
            "prerelease": False,
        },
    )
    assert created is not None
    return created


def set_commit_status(repository: str, sha: str, state: str, description: str, context: str, target_url: str = "") -> dict:
    if state not in {"pending", "success", "failure", "error"}:
        raise GitHubError(f"invalid commit status: {state}")
    return _request("POST", f"/repos/{repository}/statuses/{sha}", {"state": state, "description": description, "context": context, "target_url": target_url or None})


def _git_environment() -> dict[str, str]:
    env = os.environ.copy()
    token = env.get("GITHUB_TOKEN", "")
    if token:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!f() { printf 'username=x-access-token\\npassword=%s\\n' \"$GITHUB_TOKEN\"; }; f",
            "GIT_TERMINAL_PROMPT": "0",
        })
    return env


def _configure_identity(
    cwd: str,
    env: dict[str, str],
    name: str = "happyladysauce-ci",
    email: str = "happyladysauce-ci@noreply.local",
) -> None:
    subprocess.run(["git", "config", "user.name", name], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "config", "user.email", email], cwd=cwd, check=True, env=env)


def fast_forward_main(
    cwd: str = ".",
    *,
    branch: str = "main",
    development_branch: str = "dev",
    identity_name: str = "happyladysauce-ci",
    identity_email: str = "happyladysauce-ci@noreply.local",
) -> None:
    env = _git_environment()
    _configure_identity(cwd, env, identity_name, identity_email)
    # actions/checkout fetch-depth: 1 leaves .git/shallow, so origin/main is
    # not an ancestor of HEAD until the clone is unshallowed.
    # actions/checkout 的 fetch-depth: 1 会留下 .git/shallow，不 unshallow
    # 就无法证明 origin/main 是 HEAD 的祖先。
    if (Path(cwd) / ".git" / "shallow").is_file():
        subprocess.run(["git", "fetch", "--unshallow", "origin"], cwd=cwd, check=True, env=env)
    fetch_branches = ["origin", branch]
    if development_branch != branch:
        fetch_branches.append(development_branch)
    subprocess.run(["git", "fetch", *fetch_branches], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "merge-base", "--is-ancestor", f"origin/{branch}", "HEAD"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=cwd, check=True, env=env)


def create_and_push_tag(
    tag: str,
    message: str,
    cwd: str = ".",
    *,
    identity_name: str = "happyladysauce-ci",
    identity_email: str = "happyladysauce-ci@noreply.local",
) -> None:
    env = _git_environment()
    _configure_identity(cwd, env, identity_name, identity_email)
    existing = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{}}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    if existing.returncode == 0:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True, env=env
        ).stdout.strip()
        if existing.stdout.strip() != head:
            raise GitHubError(f"tag {tag} already points to a different commit")
        return
    subprocess.run(["git", "tag", "-a", tag, "-m", message, "HEAD"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "push", "origin", tag], cwd=cwd, check=True, env=env)
