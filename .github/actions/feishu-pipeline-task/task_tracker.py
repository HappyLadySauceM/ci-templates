#!/usr/bin/env python3
"""Synchronize a GitHub Actions workflow run with a reusable Feishu task."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


NOTIFY_DIR = Path(__file__).resolve().parents[1] / "feishu-notify"
sys.path.insert(0, str(NOTIFY_DIR))
from notify import format_duration, summarize_ci_greeting  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("feishu-pipeline-task")

BOARD_STATES = ("未触发", "执行中", "执行完毕", "执行出错")
SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
ERROR_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale"}
)
BOT_SUFFIXES = ("[bot]", "-bot")
RETRYABLE_STATUS_CODES = frozenset({408, 429}) | frozenset(range(500, 600))
RETRY_DELAYS = (1, 2)
MAX_ATTEMPTS = 3
EXTRA_KIND = "github_actions_pipeline"
EXTRA_SCHEMA = 1


class TrackerError(RuntimeError):
    """Raised when a remote API or tracker invariant fails."""


class JsonApi:
    def __init__(self, base_url: str, token: str = "", user_agent: str = "feishu-pipeline-task"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: list[tuple[str, str]] | dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        url = path if path.startswith("http") else "%s%s" % (self.base_url, path)
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query, doseq=True)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self.user_agent,
        }
        if auth and self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            request = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=20) as response:
                    raw = response.read()
                parsed = json.loads(raw.decode("utf-8") or "{}")
                return parsed
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
                retryable = exc.code in RETRYABLE_STATUS_CODES
                if retryable and attempt < MAX_ATTEMPTS:
                    log.warning("%s %s returned HTTP %s; retrying", method, path, exc.code)
                else:
                    raise TrackerError(
                        "%s %s failed with HTTP %s: %s" % (method, path, exc.code, detail[:500])
                    ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise TrackerError("%s %s failed: %s" % (method, path, exc)) from exc
                log.warning("%s %s failed with %s; retrying", method, path, type(exc).__name__)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TrackerError("%s %s returned invalid JSON" % (method, path)) from exc
            time.sleep(RETRY_DELAYS[attempt - 1])
        raise AssertionError("retry loop exhausted")


class FeishuApi:
    def __init__(self, app_id: str, app_secret: str, api_url: str = "https://open.feishu.cn"):
        self.client = JsonApi(api_url)
        response = self.client.request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": app_secret},
            auth=False,
        )
        if not isinstance(response, dict):
            raise TrackerError("Feishu token request returned non-object JSON")
        token = str(response.get("tenant_access_token") or "")
        if response.get("code") not in (0, None) or not token:
            raise TrackerError("Feishu token request rejected: %s" % _safe_api_error(response))
        self.client.token = token

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: list[tuple[str, str]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.client.request(method, path, body=body, query=query)
        if not isinstance(response, dict):
            raise TrackerError("Feishu %s %s returned non-object JSON" % (method, path))
        if response.get("code") not in (0, None):
            raise TrackerError("Feishu %s %s rejected: %s" % (method, path, _safe_api_error(response)))
        data = response.get("data") or {}
        if not isinstance(data, dict):
            raise TrackerError("Feishu %s %s returned invalid data" % (method, path))
        return data

    def pages(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        item_key: str = "items",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = dict(query or {})
            params["page_size"] = 100
            if page_token:
                params["page_token"] = page_token
            data = self.call("GET", path, query=params)
            page = data.get(item_key) or []
            if not isinstance(page, list):
                raise TrackerError("Feishu pagination returned invalid %s" % item_key)
            items.extend(item for item in page if isinstance(item, dict))
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise TrackerError("Feishu pagination says has_more without page_token")


class GitHubApi:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self.client = JsonApi(api_url, token, "feishu-pipeline-task")

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        response = self.client.request("GET", path, query=query)
        return response


def _safe_api_error(response: dict[str, Any]) -> str:
    return json.dumps(
        {"code": response.get("code"), "msg": response.get("msg")},
        ensure_ascii=False,
    )


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def normalize_github_login(value: str) -> str:
    """Normalize login, @login, or a github.com profile URL for exact matching."""

    text = value.strip()
    if not text:
        return ""
    candidate = text
    parsed = urlparse(text if "://" in text else "")
    if parsed.netloc.lower() in {"github.com", "www.github.com"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    candidate = candidate.strip().lstrip("@").rstrip("/")
    if not candidate or any(char.isspace() for char in candidate):
        return ""
    return candidate.casefold()


def board_state(action: str, conclusion: str) -> str:
    if action in {"requested", "in_progress"}:
        return "执行中"
    if action != "completed":
        raise TrackerError("unsupported workflow_run action: %s" % action)
    normalized = conclusion.strip().lower()
    if normalized in SUCCESS_CONCLUSIONS:
        return "执行完毕"
    if normalized in ERROR_CONCLUSIONS:
        return "执行出错"
    raise TrackerError("unsupported completed conclusion: %s" % conclusion)


def event_version(run: dict[str, Any], state: str) -> tuple[int, int, int]:
    run_id = int(run.get("id") or 0)
    attempt = int(run.get("run_attempt") or 1)
    phase = 2 if state in {"执行完毕", "执行出错"} else 1
    return run_id, attempt, phase


def parse_extra(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_stale_or_duplicate(existing: dict[str, Any], incoming: tuple[int, int, int], state: str) -> bool:
    latest = existing.get("latest") or {}
    if not isinstance(latest, dict):
        return False
    current = (
        int(latest.get("run_id") or 0),
        int(latest.get("run_attempt") or 0),
        int(latest.get("phase") or 0),
    )
    if current > incoming:
        return True
    return current == incoming and latest.get("state") == state


def workflow_actor(run: dict[str, Any]) -> str:
    actor = run.get("actor") or run.get("triggering_actor") or {}
    return str(actor.get("login") or "") if isinstance(actor, dict) else ""


def _commit_logins(commit: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("author", "committer"):
        user = commit.get(key) or {}
        login = str(user.get("login") or "") if isinstance(user, dict) else ""
        normalized = normalize_github_login(login)
        if normalized and not normalized.endswith(BOT_SUFFIXES):
            result.add(normalized)
    return result


def contributor_logins(github: GitHubApi, repository: str, run: dict[str, Any]) -> set[str]:
    event = str(run.get("event") or "")
    if event == "pull_request":
        result: set[str] = set()
        for pull in run.get("pull_requests") or []:
            if not isinstance(pull, dict) or not pull.get("number"):
                continue
            try:
                commits = github.get(
                    "/repos/%s/pulls/%s/commits" % (repository, pull["number"]),
                    {"per_page": 100},
                )
            except TrackerError:
                log.warning("Could not list commits for pull request %s", pull["number"])
                continue
            if isinstance(commits, list):
                for commit in commits:
                    if isinstance(commit, dict):
                        result.update(_commit_logins(commit))
        return result

    if event != "push":
        return set()

    head_sha = str(run.get("head_sha") or "")
    workflow_id = run.get("workflow_id")
    branch = str(run.get("head_branch") or "")
    run_number = int(run.get("run_number") or 0)
    base_sha = ""
    if workflow_id and branch and run_number:
        try:
            listed = github.get(
                "/repos/%s/actions/workflows/%s/runs" % (repository, workflow_id),
                {"branch": branch, "event": "push", "per_page": 100},
            )
        except TrackerError:
            log.warning("Could not list earlier workflow runs; falling back to the head commit")
            listed = {}
        candidates = listed.get("workflow_runs") if isinstance(listed, dict) else []
        previous = [
            item
            for item in (candidates or [])
            if isinstance(item, dict)
            and int(item.get("run_number") or 0) < run_number
            and str(item.get("head_sha") or "") != head_sha
        ]
        if previous:
            previous.sort(key=lambda item: int(item.get("run_number") or 0), reverse=True)
            base_sha = str(previous[0].get("head_sha") or "")

    result: set[str] = set()
    if base_sha and head_sha:
        try:
            compared = github.get(
                "/repos/%s/compare/%s...%s"
                % (repository, quote(base_sha, safe=""), quote(head_sha, safe=""))
            )
        except TrackerError:
            log.warning("Could not compare workflow commits; falling back to the head commit")
            compared = {}
        commits = compared.get("commits") if isinstance(compared, dict) else []
        for commit in commits or []:
            if isinstance(commit, dict):
                result.update(_commit_logins(commit))
        if result:
            return result

    if head_sha:
        try:
            commit = github.get(
                "/repos/%s/commits/%s" % (repository, quote(head_sha, safe=""))
            )
        except TrackerError:
            log.warning("Could not load the workflow head commit; falling back to the actor")
            commit = {}
        if isinstance(commit, dict):
            result.update(_commit_logins(commit))
    return result


def _attr_values(user: dict[str, Any], attr_id: str) -> list[str]:
    values: list[str] = []
    for attr in user.get("custom_attrs") or []:
        if not isinstance(attr, dict) or str(attr.get("id") or "") != attr_id:
            continue
        value = attr.get("value") or {}
        if not isinstance(value, dict):
            continue
        for key in ("text", "url", "pc_url"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                values.append(item)
    return values


def group_identity_map(feishu: FeishuApi, chat_id: str, attr_id: str) -> dict[str, str]:
    definitions = feishu.pages("/open-apis/contact/v3/custom_attrs")
    selected = [item for item in definitions if str(item.get("id") or "") == attr_id]
    if len(selected) != 1:
        raise TrackerError("configured Feishu GitHub custom attribute was not found")
    if str(selected[0].get("type") or "") not in {"TEXT", "HREF"}:
        raise TrackerError("Feishu GitHub custom attribute must be TEXT or HREF")

    members = feishu.pages(
        "/open-apis/im/v1/chats/%s/members" % quote(chat_id, safe=""),
        {"member_id_type": "open_id"},
    )
    ids = sorted(
        {
            str(item.get("member_id") or "")
            for item in members
            if str(item.get("member_id") or "")
        }
    )
    users: list[dict[str, Any]] = []
    for chunk in _chunks(ids, 50):
        query = [("user_ids", item) for item in chunk]
        query.extend(
            [("user_id_type", "open_id"), ("department_id_type", "open_department_id")]
        )
        data = feishu.call("GET", "/open-apis/contact/v3/users/batch", query=query)
        page = data.get("items") or []
        users.extend(item for item in page if isinstance(item, dict))

    candidates: dict[str, set[str]] = {}
    for user in users:
        open_id = str(user.get("open_id") or "")
        if not open_id:
            continue
        for value in _attr_values(user, attr_id):
            login = normalize_github_login(value)
            if login:
                candidates.setdefault(login, set()).add(open_id)

    result: dict[str, str] = {}
    for login, open_ids in candidates.items():
        if len(open_ids) == 1:
            result[login] = next(iter(open_ids))
        else:
            log.warning("GitHub login %s is duplicated in Feishu custom attributes; skipping", login)
    return result


def _run_url(repository: str, run: dict[str, Any]) -> str:
    return str(run.get("html_url") or "https://github.com/%s/actions/runs/%s" % (repository, run.get("id") or ""))


def _display_title(run: dict[str, Any]) -> str:
    return str(run.get("display_title") or run.get("name") or "CI").splitlines()[0].strip()


def failed_job_names(github: GitHubApi, repository: str, run: dict[str, Any]) -> list[str]:
    run_id = int(run.get("id") or 0)
    if not run_id:
        return []
    try:
        response = github.get("/repos/%s/actions/runs/%s/jobs" % (repository, run_id), {"per_page": 100})
    except TrackerError:
        log.warning("Could not list failed jobs for workflow run %s", run_id)
        return []
    jobs = response.get("jobs") if isinstance(response, dict) else []
    return [
        str(job.get("name") or "")
        for job in jobs or []
        if isinstance(job, dict)
        and str(job.get("conclusion") or "").lower() in ERROR_CONCLUSIONS
        and str(job.get("name") or "")
    ][:10]


def inline_workflow_run(
    github: GitHubApi,
    repository: str,
    run_id: str,
    phase: str,
    conclusion: str = "",
) -> tuple[dict[str, Any], str]:
    """Load the active run and overlay state unavailable until it completes."""
    normalized_phase = phase.strip().lower()
    if normalized_phase not in {"in_progress", "completed"}:
        raise TrackerError("TRACKER_PHASE must be in_progress or completed")
    if not run_id.isdigit() or int(run_id) < 1:
        raise TrackerError("GITHUB_RUN_ID must be a positive integer for inline sync")
    response = github.get("/repos/%s/actions/runs/%s" % (repository, run_id))
    if not isinstance(response, dict) or not response:
        raise TrackerError("GitHub workflow run lookup returned no run")
    run = dict(response)
    if normalized_phase == "in_progress":
        run["conclusion"] = ""
        return run, "in_progress"
    normalized_conclusion = conclusion.strip().lower()
    board_state("completed", normalized_conclusion)
    run["conclusion"] = normalized_conclusion
    # notify runs before GitHub marks the enclosing workflow complete.
    run["updated_at"] = datetime.now().astimezone().isoformat()
    return run, "completed"


def task_description(
    repository: str,
    run: dict[str, Any],
    state: str,
    greeting: str = "",
    failed_jobs: list[str] | None = None,
) -> str:
    started = str(run.get("run_started_at") or run.get("created_at") or "")
    ended = str(run.get("updated_at") or datetime.now().astimezone().isoformat())
    duration = format_duration(started, ended) if started and state in {"执行完毕", "执行出错"} else ""
    lines = [
        _display_title(run),
        "• Workflow: %s" % str(run.get("name") or ""),
        "• Status: %s" % state,
        "• Conclusion: %s" % str(run.get("conclusion") or ""),
        "• Branch: %s" % str(run.get("head_branch") or ""),
        "• Actor: %s" % workflow_actor(run),
        "• Run: #%s / attempt %s" % (run.get("run_number") or "", run.get("run_attempt") or 1),
    ]
    if duration:
        lines.append("• Duration: %s" % duration)
    if failed_jobs:
        lines.append("• Failed jobs: %s" % "、".join(failed_jobs))
    lines.append("• Open run: %s" % _run_url(repository, run))
    if greeting:
        lines.extend(["", "DeepSeek：%s" % greeting])
    return "\n".join(lines)[:3000]


class PipelineTracker:
    def __init__(
        self,
        feishu: FeishuApi,
        github: GitHubApi,
        *,
        repository: str,
        workflow_name: str,
        chat_id: str,
        attr_id: str,
        tasklist_name: str,
    ):
        self.feishu = feishu
        self.github = github
        self.repository = repository
        self.workflow_name = workflow_name
        self.chat_id = chat_id
        self.attr_id = attr_id
        self.tasklist_name = tasklist_name

    def ensure_board(self) -> tuple[dict[str, Any], dict[str, str]]:
        lists = self.feishu.pages("/open-apis/task/v2/tasklists")
        matching = [item for item in lists if str(item.get("name") or "") == self.tasklist_name]
        if len(matching) > 1:
            raise TrackerError("multiple Feishu task lists are named %s" % self.tasklist_name)
        if matching:
            tasklist = matching[0]
            members = tasklist.get("members") or []
            chat = [
                item
                for item in members
                if isinstance(item, dict)
                and item.get("type") == "chat"
                and item.get("id") == self.chat_id
            ]
            if not chat:
                self.feishu.call(
                    "POST",
                    "/open-apis/task/v2/tasklists/%s/add_members" % tasklist["guid"],
                    body={"members": [{"id": self.chat_id, "type": "chat", "role": "editor"}]},
                    query={"user_id_type": "open_id"},
                )
            elif chat[0].get("role") != "editor":
                self.feishu.call(
                    "POST",
                    "/open-apis/task/v2/tasklists/%s/remove_members" % tasklist["guid"],
                    body={"members": [{"id": self.chat_id, "type": "chat", "role": chat[0].get("role") or "viewer"}]},
                    query={"user_id_type": "open_id"},
                )
                self.feishu.call(
                    "POST",
                    "/open-apis/task/v2/tasklists/%s/add_members" % tasklist["guid"],
                    body={"members": [{"id": self.chat_id, "type": "chat", "role": "editor"}]},
                    query={"user_id_type": "open_id"},
                )
        else:
            data = self.feishu.call(
                "POST",
                "/open-apis/task/v2/tasklists",
                body={
                    "name": self.tasklist_name,
                    "members": [{"id": self.chat_id, "type": "chat", "role": "editor"}],
                },
                query={"user_id_type": "open_id"},
            )
            tasklist = data.get("tasklist") or {}
            if not tasklist.get("guid"):
                raise TrackerError("Feishu create task list response omitted guid")

        sections = self.feishu.pages(
            "/open-apis/task/v2/sections",
            {"resource_type": "tasklist", "resource_id": tasklist["guid"]},
        )
        by_name: dict[str, list[dict[str, Any]]] = {}
        for section in sections:
            by_name.setdefault(str(section.get("name") or ""), []).append(section)
        resolved: dict[str, str] = {}
        previous_guid = ""
        for name in BOARD_STATES:
            matches = by_name.get(name, [])
            if len(matches) > 1:
                raise TrackerError("multiple Feishu sections are named %s" % name)
            if matches:
                guid = str(matches[0].get("guid") or "")
            else:
                body = {
                    "name": name,
                    "resource_type": "tasklist",
                    "resource_id": tasklist["guid"],
                }
                if previous_guid:
                    body["insert_after"] = previous_guid
                data = self.feishu.call("POST", "/open-apis/task/v2/sections", body=body)
                section = data.get("section") or {}
                guid = str(section.get("guid") or "")
                if not guid:
                    raise TrackerError("Feishu create section response omitted guid for %s" % name)
            resolved[name] = guid
            previous_guid = guid
        return tasklist, resolved

    def find_task(self, tasklist_guid: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        summaries = self.feishu.pages(
            "/open-apis/task/v2/tasklists/%s/tasks" % tasklist_guid,
        )
        title = "CICD：%s" % self.repository
        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for summary in summaries:
            if str(summary.get("summary") or "") != title or not summary.get("guid"):
                continue
            data = self.feishu.call(
                "GET", "/open-apis/task/v2/tasks/%s" % summary["guid"], query={"user_id_type": "open_id"}
            )
            task = data.get("task") or {}
            extra = parse_extra(task.get("extra"))
            if (
                extra.get("kind") == EXTRA_KIND
                and extra.get("repository") == self.repository
                and extra.get("workflow") == self.workflow_name
            ):
                matched.append((task, extra))
        if len(matched) > 1:
            raise TrackerError("multiple managed Feishu tasks match %s / %s" % (self.repository, self.workflow_name))
        return matched[0] if matched else (None, {})

    def create_task(
        self,
        tasklist_guid: str,
        section_guid: str,
        *,
        description: str,
        extra: dict[str, Any],
        followers: list[str],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": "CICD：%s" % self.repository,
            "description": description,
            "completed_at": "0",
            "origin": {
                "platform_i18n_name": {"en_us": "GitHub Actions", "zh_cn": "GitHub Actions"},
                "href": {
                    "url": "https://github.com/%s/actions" % self.repository,
                    "title": self.workflow_name,
                },
            },
            "extra": json.dumps(extra, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "tasklists": [{"tasklist_guid": tasklist_guid, "section_guid": section_guid}],
        }
        if followers:
            body["members"] = [
                {"id": item, "type": "user", "role": "follower"} for item in followers[:50]
            ]
        data = self.feishu.call(
            "POST", "/open-apis/task/v2/tasks", body=body, query={"user_id_type": "open_id"}
        )
        task = data.get("task") or {}
        if not task.get("guid"):
            raise TrackerError("Feishu create task response omitted guid")
        return task

    def provision(self) -> dict[str, Any]:
        tasklist, sections = self.ensure_board()
        task, extra = self.find_task(str(tasklist["guid"]))
        if task is None:
            extra = {
                "schema": EXTRA_SCHEMA,
                "kind": EXTRA_KIND,
                "repository": self.repository,
                "workflow": self.workflow_name,
                "managed_followers": [],
                "latest": {"run_id": 0, "run_attempt": 0, "phase": 0, "state": "未触发"},
            }
            task = self.create_task(
                str(tasklist["guid"]),
                sections["未触发"],
                description="流水线尚未触发。",
                extra=extra,
                followers=[],
            )
        return {
            "tasklist_guid": str(tasklist["guid"]),
            "task_guid": str(task["guid"]),
            "state": str((extra.get("latest") or {}).get("state") or "未触发"),
            "matched_users": len(extra.get("managed_followers") or []),
        }

    def sync(self, run: dict[str, Any], action: str) -> dict[str, Any]:
        state = board_state(action, str(run.get("conclusion") or ""))
        incoming = event_version(run, state)
        tasklist, sections = self.ensure_board()
        task, extra = self.find_task(str(tasklist["guid"]))
        if task is not None and is_stale_or_duplicate(extra, incoming, state):
            log.info("Ignoring stale or duplicate event version=%s state=%s", incoming, state)
            return {
                "tasklist_guid": str(tasklist["guid"]),
                "task_guid": str(task["guid"]),
                "state": str((extra.get("latest") or {}).get("state") or state),
                "matched_users": len(extra.get("managed_followers") or []),
            }

        identities = group_identity_map(self.feishu, self.chat_id, self.attr_id)
        contributors = contributor_logins(self.github, self.repository, run)
        desired = sorted({identities[item] for item in contributors if item in identities})
        actor = normalize_github_login(workflow_actor(run))
        if not desired and actor in identities:
            desired = [identities[actor]]
        if len(desired) > 50:
            log.warning("Matched %d users; Feishu supports 50 task members, truncating", len(desired))
            desired = desired[:50]

        greeting = ""
        if state in {"执行完毕", "执行出错"}:
            started = str(run.get("run_started_at") or run.get("created_at") or "")
            ended = str(run.get("updated_at") or datetime.now().astimezone().isoformat())
            duration = format_duration(started, ended) if started else ""
            greeting = summarize_ci_greeting(
                _display_title(run),
                duration,
                str(run.get("head_branch") or ""),
                str(run.get("conclusion") or "unknown"),
            )

        extra = dict(extra)
        old_followers = {
            str(item) for item in extra.get("managed_followers") or [] if str(item)
        }
        extra.update(
            {
                "schema": EXTRA_SCHEMA,
                "kind": EXTRA_KIND,
                "repository": self.repository,
                "workflow": self.workflow_name,
                "managed_followers": desired,
                "latest": {
                    "run_id": incoming[0],
                    "run_attempt": incoming[1],
                    "phase": incoming[2],
                    "state": state,
                },
            }
        )
        failures = failed_job_names(self.github, self.repository, run) if state == "执行出错" else []
        description = task_description(self.repository, run, state, greeting, failures)
        if task is None:
            task = self.create_task(
                str(tasklist["guid"]),
                sections[state],
                description=description,
                extra=extra,
                followers=desired,
            )
        else:
            task_guid = str(task["guid"])
            self.feishu.call(
                "POST",
                "/open-apis/task/v2/tasks/%s/add_tasklist" % task_guid,
                body={"tasklist_guid": tasklist["guid"], "section_guid": sections[state]},
                query={"user_id_type": "open_id"},
            )
            current_followers = {
                str(item.get("id") or "")
                for item in task.get("members") or []
                if isinstance(item, dict)
                and item.get("type", "user") == "user"
                and item.get("role") == "follower"
                and item.get("id")
            }
            remove = sorted((old_followers - set(desired)) & current_followers)
            add = sorted(set(desired) - current_followers)
            if remove:
                self.feishu.call(
                    "POST",
                    "/open-apis/task/v2/tasks/%s/remove_members" % task_guid,
                    body={"members": [{"id": item, "type": "user", "role": "follower"} for item in remove]},
                    query={"user_id_type": "open_id"},
                )
            if add:
                self.feishu.call(
                    "POST",
                    "/open-apis/task/v2/tasks/%s/add_members" % task_guid,
                    body={
                        "members": [
                            {"id": item, "type": "user", "role": "follower"}
                            for item in add
                        ]
                    },
                    query={"user_id_type": "open_id"},
                )
            # Persist the processed event last. If any operation above fails, the
            # next delivery can inspect actual members and retry safely.
            self.feishu.call(
                "PATCH",
                "/open-apis/task/v2/tasks/%s" % task_guid,
                body={
                    "task": {
                        "summary": "CICD：%s" % self.repository,
                        "description": description,
                        "completed_at": "0",
                        "extra": json.dumps(
                            extra,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                    "update_fields": ["summary", "description", "completed_at", "extra"],
                },
                query={"user_id_type": "open_id"},
            )

        return {
            "tasklist_guid": str(tasklist["guid"]),
            "task_guid": str(task["guid"]),
            "state": state,
            "matched_users": len(desired),
        }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TrackerError("%s is required" % name)
    return value


def _write_outputs(result: dict[str, Any]) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        for key in ("tasklist_guid", "task_guid", "state", "matched_users"):
            handle.write("%s=%s\n" % (key.replace("_", "-"), result.get(key, "")))


def main() -> int:
    try:
        operation = os.environ.get("TRACKER_OPERATION", "sync").strip().lower()
        if operation not in {"provision", "sync"}:
            raise TrackerError("TRACKER_OPERATION must be provision or sync")
        repository = _required_env("TRACKER_REPOSITORY")
        workflow_name = _required_env("TRACKER_WORKFLOW_NAME")
        feishu = FeishuApi(_required_env("FEISHU_APP_ID"), _required_env("FEISHU_APP_SECRET"))
        github = GitHubApi(os.environ.get("GITHUB_TOKEN", "").strip())
        tracker = PipelineTracker(
            feishu,
            github,
            repository=repository,
            workflow_name=workflow_name,
            chat_id=_required_env("FEISHU_CHAT_ID"),
            attr_id=_required_env("FEISHU_GITHUB_CUSTOM_ATTR_ID"),
            tasklist_name=os.environ.get("FEISHU_TASKLIST_NAME", "CICD 流水线").strip() or "CICD 流水线",
        )
        phase = os.environ.get("TRACKER_PHASE", "").strip()
        if operation == "provision":
            result = tracker.provision()
        elif phase:
            run, action = inline_workflow_run(
                github,
                repository,
                _required_env("GITHUB_RUN_ID"),
                phase,
                os.environ.get("TRACKER_CONCLUSION", ""),
            )
            result = tracker.sync(run, action)
        else:
            path = _required_env("GITHUB_EVENT_PATH")
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            run = payload.get("workflow_run") or {}
            if not isinstance(run, dict) or not run:
                raise TrackerError("workflow_run payload is required for sync")
            result = tracker.sync(run, str(payload.get("action") or ""))
        _write_outputs(result)
        log.info(
            "Feishu pipeline task synchronized state=%s task=%s followers=%s",
            result["state"],
            result["task_guid"],
            result["matched_users"],
        )
        return 0
    except (TrackerError, OSError, ValueError, TypeError, json.JSONDecodeError):
        log.exception("Feishu pipeline task synchronization failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
