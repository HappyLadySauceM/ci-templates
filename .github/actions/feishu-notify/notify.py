#!/usr/bin/env python3
"""Send a signed Feishu custom-bot card for the current GitHub Actions event."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("feishu-notify")

NOTIFY_WORKFLOW_NAME = "feishu-notify"
BOT_ACTORS = frozenset(
    {
        "github-actions[bot]",
        "dependabot[bot]",
        "renovate[bot]",
    }
)
BOT_EVENT_NAMES = frozenset({"issue_comment", "pull_request_review"})
CI_EVENT_NAMES = frozenset({"push", "workflow_dispatch", "workflow_run"})
HEADER_TEMPLATES = {
    "green": "green",
    "red": "red",
    "orange": "orange",
    "grey": "grey",
    "blue": "blue",
}
_VERSION_HEADING = re.compile(r"^#{1,6}\s*v?\d+\.\d+(?:\.\d+)?\b.*$", re.IGNORECASE)
DEEPSEEK_TIMEOUT_SECONDS = 15
DEEPSEEK_MAX_ATTEMPTS = 3
DEEPSEEK_RETRY_DELAYS = (1, 2)
DEEPSEEK_GREETING_MAX_TOKENS = (1024, 2048, 4096)
DEEPSEEK_RETRYABLE_STATUS_CODES = frozenset({408, 429}) | frozenset(range(500, 600))
CI_GREETING_FALLBACKS = {
    "success": "流水线已成功完成，辛苦了，记得稍作休息。",
    "failure": "流水线执行失败，请点击 Open run 查看失败步骤。",
    "cancelled": "流水线已取消，请点击 Open run 查看执行记录。",
}


class NotifyError(RuntimeError):
    """Raised when the Feishu webhook rejects the request."""

    # 飞书 webhook 拒绝请求时抛出。


def gen_sign(timestamp: int | str, secret: str) -> str:
    """Return the Feishu custom-bot signature for timestamp and secret.

    Official algorithm: HMAC-SHA256 key is "{timestamp}\\n{secret}", message is empty,
    then Base64-encode the digest.
    官方算法：HMAC-SHA256 的密钥是 "{timestamp}\\n{secret}"，对空消息做摘要后再 Base64。
    """

    string_to_sign = "{}\n{}".format(timestamp, secret)
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _actor(payload: dict[str, Any]) -> str:
    for path in (
        ("sender", "login"),
        ("comment", "user", "login"),
        ("review", "user", "login"),
        ("workflow_run", "actor", "login"),
        ("pull_request", "user", "login"),
        ("issue", "user", "login"),
    ):
        node: Any = payload
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node:
            return node
    return "unknown"


def _repo(payload: dict[str, Any]) -> str:
    repository = payload.get("repository") or {}
    if isinstance(repository, dict):
        name = repository.get("full_name")
        if isinstance(name, str) and name:
            return name
    return os.environ.get("GITHUB_REPOSITORY", "unknown")


def skip_reason(event_name: str, payload: dict[str, Any]) -> str | None:
    """Return an English skip reason, or None when the event should be posted.

    返回跳过原因（英文）；需要推送时返回 None。
    """

    if event_name == "workflow_run":
        workflow = payload.get("workflow") or {}
        run = payload.get("workflow_run") or {}
        names = {
            str(workflow.get("name") or ""),
            str(run.get("name") or ""),
        }
        if NOTIFY_WORKFLOW_NAME in names:
            return "skip notify workflow_run recursion"
        if str(run.get("conclusion") or "") == "skipped":
            return "skip workflow_run with skipped conclusion"

    if event_name in BOT_EVENT_NAMES and _actor(payload) in BOT_ACTORS:
        return "skip bot actor on %s" % event_name
    return None


def _buttons(items: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary",
                "url": url,
            }
            for label, url in items
            if url
        ],
    }


def _card(title: str, color: str, lines: list[str], buttons: list[tuple[str, str]]) -> dict[str, Any]:
    body = "\n".join(line for line in lines if line)
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": body[:4000]},
        },
    ]
    action = _buttons(buttons)
    if action["actions"]:
        elements.append(action)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:100]},
                "template": HEADER_TEMPLATES.get(color, "blue"),
            },
            "elements": elements,
        },
    }


def _workflow_color(conclusion: str) -> str:
    return {
        "success": "green",
        "failure": "red",
        "cancelled": "orange",
        "timed_out": "red",
        "action_required": "orange",
    }.get(conclusion, "grey")


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def strip_release_version_heading(text: str) -> str:
    """Drop leading Markdown version headings such as # v0.1.1.

    去掉正文开头的版本标题（例如 # v0.1.1），GitHub Release 名已经有 tag。
    """

    lines = text.splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if _VERSION_HEADING.match(first):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def format_release_notes(body: str, limit: int = 1500) -> str:
    """Keep newlines and turn Markdown headings into lark_md bold.

    保留换行，并把 Markdown 标题转成飞书 lark_md 粗体。
    """

    text = strip_release_version_heading(body)
    formatted: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            formatted.append("**%s**" % heading.group(2).strip())
        else:
            formatted.append(line)
    text = "\n".join(formatted).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def format_duration(started_at: str, ended_at: str) -> str:
    """Format a duration the way GitHub Actions summary does, e.g. 42m 9s.

    把耗时格式化成 GitHub Actions Summary 那种 42m 9s。
    """

    seconds = max(0, int((_parse_time(ended_at) - _parse_time(started_at)).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if secs or not parts:
        parts.append("%ds" % secs)
    return " ".join(parts)


def previous_release_tag(releases: list[Any], current_tag: str) -> str | None:
    """Return the published tag before current_tag, skipping drafts.

    返回 current_tag 之前最近一条已发布 tag，跳过 draft。
    """

    tags: list[str] = []
    for item in releases:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = item.get("tag_name")
        if isinstance(tag, str) and tag:
            tags.append(tag)
    if current_tag in tags:
        idx = tags.index(current_tag)
        if idx + 1 < len(tags):
            return tags[idx + 1]
        return None
    return tags[0] if tags else None


def github_get(path: str) -> Any:
    """GET a GitHub REST path and return parsed JSON.

    GET GitHub REST 路径并返回解析后的 JSON。
    """

    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    url = path if path.startswith("http") else "%s%s" % (api, path)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "feishu-notify",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise NotifyError("GitHub HTTP %s: %s" % (exc.code, detail)) from exc
    except URLError as exc:
        raise NotifyError("GitHub request failed: %s" % exc.reason) from exc
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except json.JSONDecodeError as exc:
        raise NotifyError("GitHub returned non-JSON: %s" % raw[:200]) from exc


def _repo_html(payload: dict[str, Any], repo: str) -> str:
    repository = payload.get("repository") or {}
    if isinstance(repository, dict):
        url = repository.get("html_url")
        if isinstance(url, str) and url:
            return url
    return "https://github.com/%s" % repo


def summarize_ci_greeting(title: str, duration: str, branch: str, conclusion: str) -> str:
    """Ask DeepSeek for a short Chinese greeting with a deterministic fallback.

    向 DeepSeek 要一句中文问候；密钥缺失或请求失败时使用固定中文回退文案。
    """

    fallback = CI_GREETING_FALLBACKS.get(
        conclusion,
        "流水线已结束，请点击 Open run 查看详情。",
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        log.warning("DeepSeek CI greeting unavailable: API key is missing; using fallback")
        return fallback
    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    prompt = {
        "instruction": (
            "用简体中文写 1 到 3 句给程序员看的飞书问候。"
            "成功时轻松祝贺并提醒劳逸结合；失败时稳住并建议先点 Open run。"
            "只根据给定字段，不要编造未提供的事实，不要提密钥或内部路径。"
        ),
        "title": title,
        "duration": duration,
        "branch": branch,
        "conclusion": conclusion,
    }
    for attempt in range(1, DEEPSEEK_MAX_ATTEMPTS + 1):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            "temperature": 0.7,
            "max_tokens": DEEPSEEK_GREETING_MAX_TOKENS[attempt - 1],
        }
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer %s" % api_key,
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:
                raw = response.read()
            parsed = json.loads(raw.decode("utf-8") or "{}")
            choices = parsed.get("choices") if isinstance(parsed, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("response has no choices")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise ValueError("response has no message")
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                finish_reason = choices[0].get("finish_reason", "unknown")
                raise ValueError("response content is empty (finish_reason=%s)" % finish_reason)
            return text.strip()[:500]
        except HTTPError as exc:
            retryable = exc.code in DEEPSEEK_RETRYABLE_STATUS_CODES
            log.warning(
                "DeepSeek CI greeting attempt %d/%d failed: HTTP %s%s",
                attempt,
                DEEPSEEK_MAX_ATTEMPTS,
                exc.code,
                "; retrying" if retryable and attempt < DEEPSEEK_MAX_ATTEMPTS else "; using fallback",
            )
            if not retryable:
                break
        except (URLError, TimeoutError, ValueError, TypeError, KeyError, IndexError) as exc:
            log.warning(
                "DeepSeek CI greeting attempt %d/%d failed: %s%s",
                attempt,
                DEEPSEEK_MAX_ATTEMPTS,
                type(exc).__name__,
                "; retrying" if attempt < DEEPSEEK_MAX_ATTEMPTS else "; using fallback",
            )
        except Exception as exc:  # Keep notification alive for unexpected client failures.
            log.warning(
                "DeepSeek CI greeting attempt %d/%d failed: %s%s",
                attempt,
                DEEPSEEK_MAX_ATTEMPTS,
                type(exc).__name__,
                "; retrying" if attempt < DEEPSEEK_MAX_ATTEMPTS else "; using fallback",
            )
        if attempt < DEEPSEEK_MAX_ATTEMPTS:
            time.sleep(DEEPSEEK_RETRY_DELAYS[attempt - 1])
    log.warning("DeepSeek CI greeting unavailable; using fallback")
    return fallback


def _ci_display_title(payload: dict[str, Any], run: dict[str, Any]) -> str:
    display = str(run.get("display_title") or "").strip()
    if display:
        return display
    message = str((payload.get("head_commit") or {}).get("message") or "").splitlines()
    first = message[0].strip() if message else str(run.get("name") or "CI")
    number = run.get("run_number") or os.environ.get("GITHUB_RUN_NUMBER", "")
    if number:
        return "%s #%s" % (first, number)
    return first


def _ci_card(
    payload: dict[str, Any],
    run: dict[str, Any],
    now: str | None,
    greeting: str = "",
) -> dict[str, Any]:
    repo = _repo(payload)
    actor = _actor(payload)
    conclusion = str(run.get("conclusion") or os.environ.get("FEISHU_CONCLUSION") or "unknown")
    display = _ci_display_title(payload, run)
    url = str(run.get("html_url") or "")
    if not url:
        run_id = run.get("id") or os.environ.get("GITHUB_RUN_ID", "")
        if run_id:
            url = "https://github.com/%s/actions/runs/%s" % (repo, run_id)
    started = str(run.get("run_started_at") or run.get("created_at") or "")
    created = str(run.get("created_at") or started)
    ended = now or datetime.now().astimezone().isoformat()
    duration = format_duration(started, ended) if started else ""
    header = ("CICD：%s" % repo)[:100]
    lines = ["**%s**" % display]
    if greeting:
        lines.append(greeting)
    lines.extend(
        [
            "- **Workflow:** %s" % str(run.get("name") or ""),
            "- **Conclusion:** %s" % conclusion,
            "- **Triggered:** %s" % created,
            "- **Total duration:** %s" % duration,
            "- **Branch:** %s" % str(run.get("head_branch") or ""),
            "- **Actor:** %s" % actor,
            "- **Event:** %s" % str(run.get("event") or ""),
        ]
    )
    return _card(header, _workflow_color(conclusion), lines, [("Open run", url)])


def build_card(
    event_name: str,
    payload: dict[str, Any],
    *,
    run: dict[str, Any] | None = None,
    artifacts: Any = None,
    previous_tag: str | None = None,
    now: str | None = None,
    kind: str = "",
    greeting: str = "",
) -> dict[str, Any]:
    """Build an inline Feishu interactive card for a GitHub event.

    为 GitHub 事件构造内联飞书交互卡片。
    """

    del artifacts
    repo = _repo(payload)
    actor = _actor(payload)
    if kind == "release":
        event_name = "release"
    use_ci = kind == "ci" or (kind != "release" and event_name in CI_EVENT_NAMES)
    if use_ci:
        resolved = dict(run or {})
        if event_name == "workflow_run" and not resolved:
            wf_run = payload.get("workflow_run") or {}
            if isinstance(wf_run, dict):
                resolved = dict(wf_run)
        return _ci_card(payload, resolved, now, greeting=greeting)

    if event_name == "pull_request":
        pr = payload.get("pull_request") or {}
        action = str(payload.get("action") or "")
        number = pr.get("number")
        title = str(pr.get("title") or "")
        url = str(pr.get("html_url") or "")
        merged = bool(pr.get("merged"))
        if action == "closed" and merged:
            label = "merged"
            color = "green"
        elif action == "closed":
            label = "closed"
            color = "grey"
        else:
            label = action or "updated"
            color = "blue"
        return _card(
            "PR %s · %s #%s" % (label, repo, number),
            color,
            [
                "**Title:** %s" % title,
                "**Actor:** %s" % actor,
                "**Action:** %s" % label,
            ],
            [("Open", url)],
        )

    if event_name == "pull_request_review":
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        state = str(review.get("state") or "commented")
        url = str(review.get("html_url") or pr.get("html_url") or "")
        color = {"approved": "green", "changes_requested": "red"}.get(state, "blue")
        return _card(
            "PR review %s · %s #%s" % (state, repo, pr.get("number")),
            color,
            [
                "**Title:** %s" % str(pr.get("title") or ""),
                "**Reviewer:** %s" % actor,
                "**State:** %s" % state,
            ],
            [("Open", url)],
        )

    if event_name in {"issues", "issue_comment"}:
        issue = payload.get("issue") or {}
        action = str(payload.get("action") or "")
        number = issue.get("number")
        title = str(issue.get("title") or "")
        if event_name == "issue_comment":
            comment = payload.get("comment") or {}
            url = str(comment.get("html_url") or issue.get("html_url") or "")
            body = _truncate(str(comment.get("body") or ""))
            return _card(
                "Issue comment · %s #%s" % (repo, number),
                "blue",
                [
                    "**Title:** %s" % title,
                    "**Actor:** %s" % actor,
                    "**Comment:** %s" % body,
                ],
                [("Open", url)],
            )
        url = str(issue.get("html_url") or "")
        color = "grey" if action == "closed" else "blue"
        return _card(
            "Issue %s · %s #%s" % (action, repo, number),
            color,
            [
                "**Title:** %s" % title,
                "**Actor:** %s" % actor,
                "**Action:** %s" % action,
            ],
            [("Open", url)],
        )

    if event_name == "release":
        release = payload.get("release") or {}
        tag = str(release.get("tag_name") or "")
        name = str(release.get("name") or tag)
        url = str(release.get("html_url") or "")
        body = str(release.get("body") or "").strip()
        repo_url = _repo_html(payload, repo)
        lines = [
            "**Name:** %s" % name,
            "**Tag:** %s" % tag,
            "**Actor:** %s" % actor,
        ]
        notes = format_release_notes(body)
        if notes:
            lines.append("**Notes:**\n%s" % notes)
        buttons = [("Open release", url)]
        if previous_tag:
            buttons.append(("Compare", "%s/compare/%s...%s" % (repo_url, previous_tag, tag)))
        elif tag:
            buttons.append(("Tag commit", "%s/commits/%s" % (repo_url, tag)))
        return _card("Release %s · %s" % (tag, repo), "blue", lines, buttons)

    url = str((payload.get("repository") or {}).get("html_url") or "")
    return _card(
        "%s · %s" % (event_name, repo),
        "blue",
        ["**Actor:** %s" % actor, "**Event:** %s" % event_name],
        [("Open", url)],
    )


def post_card(
    webhook: str,
    secret: str,
    card: dict[str, Any],
    now: int | None = None,
) -> None:
    """POST a signed card to the Feishu custom-bot webhook.

    把带签名的卡片 POST 到飞书自定义机器人 webhook。
    urllib respects HTTPS_PROXY / HTTP_PROXY from the runner environment.
    urllib 会使用 runner 环境里的 HTTPS_PROXY / HTTP_PROXY。
    """

    timestamp = int(time.time() if now is None else now)
    body = dict(card)
    body["timestamp"] = str(timestamp)
    body["sign"] = gen_sign(timestamp, secret)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise NotifyError("Feishu HTTP %s: %s" % (exc.code, detail)) from exc
    except URLError as exc:
        raise NotifyError("Feishu request failed: %s" % exc.reason) from exc

    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise NotifyError("Feishu returned non-JSON: %s" % raw[:200]) from exc
    code = parsed.get("code", parsed.get("StatusCode"))
    if code not in (0, None):
        raise NotifyError("Feishu rejected the card: %s" % parsed)
    log.info("Posted Feishu card code=%s", code)


def main(argv: list[str] | None = None) -> int:
    del argv
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not webhook:
        log.error("FEISHU_WEBHOOK_URL is missing")
        return 1
    if not secret:
        log.error("FEISHU_WEBHOOK_SECRET is missing")
        return 1
    if not event_name or not event_path:
        log.error("GITHUB_EVENT_NAME or GITHUB_EVENT_PATH is missing")
        return 1

    with open(event_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    reason = skip_reason(event_name, payload)
    if reason:
        log.info("%s", reason)
        return 0

    kind = os.environ.get("FEISHU_CARD_KIND", "").strip()
    release_tag = os.environ.get("FEISHU_RELEASE_TAG", "").strip()
    run: dict[str, Any] | None = None
    previous_tag: str | None = None
    greeting = ""
    repo = os.environ.get("GITHUB_REPOSITORY") or _repo(payload)
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if kind == "release" or release_tag:
        kind = "release"
        if event_name != "release" and release_tag:
            try:
                loaded = github_get("/repos/%s/releases/tags/%s" % (repo, release_tag))
                if isinstance(loaded, dict):
                    payload = dict(payload)
                    payload["release"] = loaded
            except NotifyError:
                log.exception("Failed to load GitHub release %s", release_tag)
        event_name = "release"
    if kind == "ci" or event_name in CI_EVENT_NAMES:
        if event_name == "workflow_run":
            wf_run = payload.get("workflow_run") or {}
            run = dict(wf_run) if isinstance(wf_run, dict) else {}
            run_id = str(run.get("id") or run_id)
        elif run_id:
            try:
                loaded = github_get("/repos/%s/actions/runs/%s" % (repo, run_id))
                run = loaded if isinstance(loaded, dict) else {}
            except NotifyError:
                log.exception("Failed to load GitHub Actions run")
                run = {}
        conclusion = os.environ.get("FEISHU_CONCLUSION", "").strip()
        if run is None:
            run = {}
        if conclusion:
            run["conclusion"] = conclusion
        display = _ci_display_title(payload, run)
        started = str(run.get("run_started_at") or run.get("created_at") or "")
        ended = datetime.now().astimezone().isoformat()
        duration = format_duration(started, ended) if started else ""
        greeting = summarize_ci_greeting(
            display,
            duration,
            str(run.get("head_branch") or ""),
            str(run.get("conclusion") or conclusion or "unknown"),
        )
    if event_name == "release":
        tag = release_tag or str((payload.get("release") or {}).get("tag_name") or "")
        try:
            loaded = github_get("/repos/%s/releases?per_page=20" % repo)
            releases = loaded if isinstance(loaded, list) else []
            previous_tag = previous_release_tag(releases, tag)
        except NotifyError:
            log.exception("Failed to load GitHub releases")

    card = build_card(
        event_name,
        payload,
        run=run,
        previous_tag=previous_tag,
        kind=kind,
        greeting=greeting,
    )
    try:
        post_card(webhook, secret, card)
    except NotifyError:
        log.exception("Failed to post Feishu card")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
