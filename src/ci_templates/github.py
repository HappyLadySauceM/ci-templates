from __future__ import annotations

import json
import os
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GitHubError(RuntimeError):
    pass


def _request(method: str, endpoint: str, body: object | None = None) -> dict:
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
    except (HTTPError, URLError) as exc:
        raise GitHubError(f"GitHub API {method} {endpoint} failed: {exc}") from exc
    return json.loads(payload) if payload else {}


def create_release(repository: str, tag: str, target: str, body: str) -> dict:
    return _request("POST", f"/repos/{repository}/releases", {"tag_name": tag, "target_commitish": target, "name": tag, "body": body, "draft": False, "prerelease": False})


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


def _configure_identity(cwd: str, env: dict[str, str]) -> None:
    subprocess.run(["git", "config", "user.name", "happyladysauce-ci"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "happyladysauce-ci@noreply.local"], cwd=cwd, check=True, env=env)


def fast_forward_main(cwd: str = ".") -> None:
    env = _git_environment()
    _configure_identity(cwd, env)
    subprocess.run(["git", "fetch", "origin", "main", "dev"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=cwd, check=True, env=env)


def create_and_push_tag(tag: str, message: str, cwd: str = ".") -> None:
    env = _git_environment()
    _configure_identity(cwd, env)
    subprocess.run(["git", "tag", "-a", tag, "-m", message, "HEAD"], cwd=cwd, check=True, env=env)
    subprocess.run(["git", "push", "origin", tag], cwd=cwd, check=True, env=env)
