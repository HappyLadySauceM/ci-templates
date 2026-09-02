#!/usr/bin/env python3
"""Send a signed Feishu custom-bot card for the current GitHub Actions event."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
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
HEADER_TEMPLATES = {
    "green": "green",
    "red": "red",
    "orange": "orange",
    "grey": "grey",
    "blue": "blue",
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


def _button(url: str, label: str = "Open") -> dict[str, Any]:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary",
                "url": url,
            }
        ],
    }


def _card(title: str, color: str, lines: list[str], url: str) -> dict[str, Any]:
    body = "\n".join(line for line in lines if line)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title[:100]},
                "template": HEADER_TEMPLATES.get(color, "blue"),
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body[:4000]},
                },
                _button(url),
            ],
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


def build_card(event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an inline Feishu interactive card for a GitHub event.

    为 GitHub 事件构造内联飞书交互卡片。
    """

    repo = _repo(payload)
    actor = _actor(payload)

    if event_name == "workflow_run":
        run = payload.get("workflow_run") or {}
        conclusion = str(run.get("conclusion") or "unknown")
        workflow_name = str(run.get("name") or (payload.get("workflow") or {}).get("name") or "workflow")
        url = str(run.get("html_url") or "")
        branch = str(run.get("head_branch") or "")
        sha = str(run.get("head_sha") or "")[:7]
        header = (
            "CI failed · %s" % repo
            if conclusion == "failure"
            else "CI %s · %s" % (conclusion, repo)
        )
        return _card(
            header,
            _workflow_color(conclusion),
            [
                "**Workflow:** %s" % workflow_name,
                "**Conclusion:** %s" % conclusion,
                "**Branch:** %s" % branch,
                "**SHA:** %s" % sha,
                "**Actor:** %s" % actor,
                "**Event:** %s" % str(run.get("event") or ""),
            ],
            url,
        )

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
            url,
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
            url,
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
                url,
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
            url,
        )

    if event_name == "release":
        release = payload.get("release") or {}
        tag = str(release.get("tag_name") or "")
        name = str(release.get("name") or tag)
        url = str(release.get("html_url") or "")
        return _card(
            "Release %s · %s" % (tag, repo),
            "green",
            [
                "**Name:** %s" % name,
                "**Tag:** %s" % tag,
                "**Actor:** %s" % actor,
            ],
            url,
        )

    url = str((payload.get("repository") or {}).get("html_url") or "")
    return _card(
        "%s · %s" % (event_name, repo),
        "blue",
        ["**Actor:** %s" % actor, "**Event:** %s" % event_name],
        url,
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

    card = build_card(event_name, payload)
    try:
        post_card(webhook, secret, card)
    except NotifyError:
        log.exception("Failed to post Feishu card")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
