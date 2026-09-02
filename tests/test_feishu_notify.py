import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from io import BytesIO


ACTION_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions" / "feishu-notify"
sys.path.insert(0, str(ACTION_DIR))

import notify  # noqa: E402


def workflow_run_payload(**overrides):
    payload = {
        "action": "completed",
        "workflow": {"name": "knowledge-core-pipeline"},
        "workflow_run": {
            "name": "knowledge-core-pipeline",
            "conclusion": "success",
            "html_url": "https://github.com/HappyLadySauceM/Knowledge-Core/actions/runs/1",
            "head_branch": "dev",
            "head_sha": "abc1234deadbeef",
            "event": "push",
            "actor": {"login": "alice"},
        },
        "repository": {"full_name": "HappyLadySauceM/Knowledge-Core"},
        "sender": {"login": "alice"},
    }
    payload.update(overrides)
    return payload


class FeishuSignTest(unittest.TestCase):
    def test_sign_matches_official_hmac_of_empty_message(self):
        # Official Feishu sample: key is "{timestamp}\n{secret}", message is empty.
        # 飞书官方示例：密钥为 "{timestamp}\n{secret}"，对空消息做 HMAC。
        sign = notify.gen_sign(1599360473, "demo")
        self.assertEqual(sign, "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")


class FeishuSkipTest(unittest.TestCase):
    def test_skips_notify_workflow_run_to_avoid_recursion(self):
        payload = workflow_run_payload(workflow={"name": "feishu-notify"})
        payload["workflow_run"]["name"] = "feishu-notify"
        reason = notify.skip_reason("workflow_run", payload)
        self.assertIsNotNone(reason)
        self.assertIn("recursion", reason)

    def test_skips_skipped_workflow_conclusion(self):
        payload = workflow_run_payload()
        payload["workflow_run"]["conclusion"] = "skipped"
        reason = notify.skip_reason("workflow_run", payload)
        self.assertIsNotNone(reason)
        self.assertIn("skipped", reason)

    def test_keeps_pipeline_workflow_run(self):
        payload = workflow_run_payload()
        self.assertIsNone(notify.skip_reason("workflow_run", payload))

    def test_skips_bot_issue_comment(self):
        payload = {
            "action": "created",
            "comment": {
                "body": "ok",
                "html_url": "https://github.com/org/repo/issues/1#issuecomment-1",
                "user": {"login": "github-actions[bot]"},
            },
            "issue": {"title": "bug", "html_url": "https://github.com/org/repo/issues/1", "number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "github-actions[bot]"},
        }
        reason = notify.skip_reason("issue_comment", payload)
        self.assertIsNotNone(reason)
        self.assertIn("bot", reason)

    def test_skips_bot_pull_request_review(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "approved",
                "html_url": "https://github.com/org/repo/pull/1#pullrequestreview-1",
                "user": {"login": "dependabot[bot]"},
            },
            "pull_request": {"title": "deps", "html_url": "https://github.com/org/repo/pull/1", "number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "dependabot[bot]"},
        }
        reason = notify.skip_reason("pull_request_review", payload)
        self.assertIsNotNone(reason)
        self.assertIn("bot", reason)

    def test_keeps_human_issue_comment(self):
        payload = {
            "action": "created",
            "comment": {
                "body": "please look",
                "html_url": "https://github.com/org/repo/issues/1#issuecomment-1",
                "user": {"login": "alice"},
            },
            "issue": {"title": "bug", "html_url": "https://github.com/org/repo/issues/1", "number": 1},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        }
        self.assertIsNone(notify.skip_reason("issue_comment", payload))


class FeishuCardTest(unittest.TestCase):
    def test_failed_workflow_header_includes_ci_failed_keyword(self):
        payload = workflow_run_payload()
        payload["workflow_run"]["conclusion"] = "failure"
        card = notify.build_card("workflow_run", payload)
        title = card["card"]["header"]["title"]["content"]
        self.assertIn("CI failed", title)
        self.assertEqual(card["card"]["header"]["template"], "red")
        self.assertEqual(card["msg_type"], "interactive")

    def test_successful_workflow_is_green_and_links_to_run(self):
        payload = workflow_run_payload()
        card = notify.build_card("workflow_run", payload)
        self.assertEqual(card["card"]["header"]["template"], "green")
        self.assertIn(
            "https://github.com/HappyLadySauceM/Knowledge-Core/actions/runs/1",
            json.dumps(card),
        )

    def test_pull_request_opened_card_includes_title(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "title": "Add notify",
                "html_url": "https://github.com/org/repo/pull/9",
                "number": 9,
                "merged": False,
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        }
        card = notify.build_card("pull_request", payload)
        dumped = json.dumps(card)
        self.assertIn("Add notify", dumped)
        self.assertIn("#9", dumped)

    def test_merged_pull_request_is_green(self):
        payload = {
            "action": "closed",
            "pull_request": {
                "title": "Add notify",
                "html_url": "https://github.com/org/repo/pull/9",
                "number": 9,
                "merged": True,
                "user": {"login": "alice"},
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        }
        card = notify.build_card("pull_request", payload)
        self.assertEqual(card["card"]["header"]["template"], "green")
        self.assertIn("merged", card["card"]["header"]["title"]["content"].lower())

    def test_issue_card_includes_number(self):
        payload = {
            "action": "opened",
            "issue": {
                "title": "Runner OOM",
                "html_url": "https://github.com/org/repo/issues/3",
                "number": 3,
                "user": {"login": "bob"},
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "bob"},
        }
        card = notify.build_card("issues", payload)
        dumped = json.dumps(card)
        self.assertIn("Runner OOM", dumped)
        self.assertIn("#3", dumped)

    def test_release_card_includes_tag(self):
        payload = {
            "action": "published",
            "release": {
                "tag_name": "v1.2.3",
                "name": "v1.2.3",
                "html_url": "https://github.com/org/repo/releases/tag/v1.2.3",
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        }
        card = notify.build_card("release", payload)
        self.assertIn("v1.2.3", json.dumps(card))
        self.assertEqual(card["card"]["header"]["template"], "green")


class FeishuPostTest(unittest.TestCase):
    def test_post_includes_timestamp_and_sign(self):
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps({"code": 0, "msg": "success"}).encode()

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return _Response()

        with patch.object(notify, "urlopen", fake_urlopen):
            notify.post_card(
                "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "super-secret",
                {"msg_type": "interactive", "card": {"header": {"title": {"content": "x"}}}},
                now=1599360473,
            )

        self.assertEqual(captured["timeout"], 15)
        self.assertEqual(captured["body"]["timestamp"], "1599360473")
        self.assertEqual(captured["body"]["sign"], notify.gen_sign(1599360473, "super-secret"))
        self.assertEqual(captured["body"]["msg_type"], "interactive")

    def test_post_rejects_nonzero_feishu_code(self):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps({"code": 19022, "msg": "Ip Not Allowed"}).encode()

        with patch.object(notify, "urlopen", lambda *args, **kwargs: _Response()):
            with self.assertRaises(notify.NotifyError) as ctx:
                notify.post_card("https://example.invalid/hook", "secret", {"msg_type": "text"})
        self.assertIn("19022", str(ctx.exception))

    def test_post_rejects_http_error(self):
        def boom(*args, **kwargs):
            raise HTTPError("https://example.invalid/hook", 500, "boom", hdrs=None, fp=BytesIO(b"err"))

        with patch.object(notify, "urlopen", boom):
            with self.assertRaises(notify.NotifyError):
                notify.post_card("https://example.invalid/hook", "secret", {"msg_type": "text"})


class FeishuMainTest(unittest.TestCase):
    def test_main_skips_without_posting(self):
        payload = workflow_run_payload(workflow={"name": "feishu-notify"})
        payload["workflow_run"]["name"] = "feishu-notify"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(payload, handle)
            event_path = handle.name
        env = {
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_EVENT_PATH": event_path,
            "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
            "FEISHU_WEBHOOK_SECRET": "secret",
        }
        try:
            with patch.dict(os.environ, env, clear=False):
                with patch.object(notify, "post_card") as post_card:
                    self.assertEqual(notify.main(), 0)
                    post_card.assert_not_called()
        finally:
            os.unlink(event_path)

    def test_main_requires_webhook(self):
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": "", "FEISHU_WEBHOOK_SECRET": "x"}, clear=False):
            self.assertEqual(notify.main(), 1)
