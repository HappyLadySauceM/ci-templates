import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch
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
    def test_failed_workflow_header_uses_cicd_prefix(self):
        payload = workflow_run_payload()
        payload["workflow_run"]["conclusion"] = "failure"
        payload["workflow_run"]["display_title"] = "break the build #7"
        card = notify.build_card("workflow_run", payload)
        title = card["card"]["header"]["title"]["content"]
        self.assertTrue(title.startswith("CICD："))
        self.assertIn("break the build #7", title)
        self.assertNotIn("CI failed", title)
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
        self.assertEqual(card["card"]["header"]["template"], "blue")

    def test_push_ci_card_includes_title_duration_and_run_link_without_artifacts(self):
        payload = {
            "head_commit": {
                "message": "fix(ci): name the Feishu workflow_run listeners GitHub requires\n\nbody",
            },
            "repository": {
                "full_name": "HappyLadySauceM/Knowledge-Core-Web",
                "html_url": "https://github.com/HappyLadySauceM/Knowledge-Core-Web",
            },
            "sender": {"login": "alice"},
        }
        run = {
            "id": 33648574717,
            "run_number": 45,
            "display_title": "fix(ci): name the Feishu workflow_run listeners GitHub requires #45",
            "html_url": "https://github.com/HappyLadySauceM/Knowledge-Core-Web/actions/runs/33648574717",
            "created_at": "2026-09-02T12:00:00Z",
            "run_started_at": "2026-09-02T12:00:05Z",
            "conclusion": "success",
            "status": "completed",
            "head_branch": "dev",
            "event": "push",
            "name": "knowledge-core-web-pipeline",
        }
        artifacts = {
            "artifacts": [
                {"name": "knowledge-core-web-verification-1", "expired": False},
                {"name": "knowledge-core-web-candidate-web-1", "expired": False},
            ]
        }
        card = notify.build_card(
            "push",
            payload,
            run=run,
            artifacts=artifacts,
            now="2026-09-02T12:42:14Z",
        )
        dumped = json.dumps(card, ensure_ascii=False)
        self.assertTrue(card["card"]["header"]["title"]["content"].startswith("CICD："))
        self.assertIn("fix(ci): name the Feishu workflow_run listeners GitHub requires #45", dumped)
        self.assertIn("42m 9s", dumped)
        self.assertIn("2026-09-02T12:00:00Z", dumped)
        self.assertNotIn("knowledge-core-web-verification-1", dumped)
        self.assertNotIn("knowledge-core-web-candidate-web-1", dumped)
        self.assertNotIn("**Artifacts:**", dumped)
        self.assertIn(
            "https://github.com/HappyLadySauceM/Knowledge-Core-Web/actions/runs/33648574717",
            dumped,
        )
        self.assertEqual(card["card"]["header"]["template"], "green")

    def test_failed_push_ci_card_uses_ci_failed_header(self):
        payload = {
            "head_commit": {"message": "break the build"},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "alice"},
        }
        run = {
            "run_number": 7,
            "display_title": "break the build #7",
            "html_url": "https://github.com/org/repo/actions/runs/9",
            "created_at": "2026-09-02T12:00:00Z",
            "run_started_at": "2026-09-02T12:00:00Z",
            "conclusion": "failure",
            "name": "knowledge-core-pipeline",
        }
        card = notify.build_card("push", payload, run=run, now="2026-09-02T12:01:00Z")
        self.assertTrue(card["card"]["header"]["title"]["content"].startswith("CICD："))
        self.assertIn("break the build #7", card["card"]["header"]["title"]["content"])
        self.assertEqual(card["card"]["header"]["template"], "red")

    def test_format_duration_matches_github_summary(self):
        self.assertEqual(
            notify.format_duration("2026-09-02T12:00:05Z", "2026-09-02T12:42:14Z"),
            "42m 9s",
        )
        self.assertEqual(
            notify.format_duration("2026-09-02T12:00:00Z", "2026-09-02T12:00:15Z"),
            "15s",
        )
        self.assertEqual(
            notify.format_duration("2026-09-02T10:00:00Z", "2026-09-02T12:03:04Z"),
            "2h 3m 4s",
        )

    def test_release_card_includes_body_and_compare_button(self):
        payload = {
            "action": "published",
            "release": {
                "tag_name": "v1.2.3",
                "name": "Web 1.2.3",
                "body": "Ship the Feishu card and bump runner capacity.\n\n- notify\n- runners",
                "html_url": "https://github.com/org/repo/releases/tag/v1.2.3",
            },
            "repository": {
                "full_name": "org/repo",
                "html_url": "https://github.com/org/repo",
            },
            "sender": {"login": "alice"},
        }
        card = notify.build_card("release", payload, previous_tag="v1.2.2")
        dumped = json.dumps(card, ensure_ascii=False)
        notes = card["card"]["elements"][0]["text"]["content"]
        self.assertIn("Ship the Feishu card and bump runner capacity.", dumped)
        self.assertIn("\n- notify\n- runners", notes)
        self.assertNotIn("v0.1.1", notes)
        self.assertEqual(card["card"]["header"]["template"], "blue")
        self.assertIn("https://github.com/org/repo/compare/v1.2.2...v1.2.3", dumped)
        self.assertIn("https://github.com/org/repo/releases/tag/v1.2.3", dumped)

    def test_release_notes_drop_duplicate_version_heading(self):
        payload = {
            "action": "published",
            "release": {
                "tag_name": "v0.1.26",
                "name": "v0.1.26",
                "body": "# v0.1.1 - 共享变更：\n\n- 调整 CI\n\n## Affected services\n\n- gateway",
                "html_url": "https://github.com/org/repo/releases/tag/v0.1.26",
            },
            "repository": {"full_name": "org/repo", "html_url": "https://github.com/org/repo"},
            "sender": {"login": "alice"},
        }
        notes = notify.build_card("release", payload)["card"]["elements"][0]["text"]["content"]
        self.assertNotIn("v0.1.1", notes)
        self.assertIn("- 调整 CI", notes)
        self.assertIn("\n", notes)

    def test_ci_card_includes_deepseek_greeting(self):
        payload = workflow_run_payload()
        card = notify.build_card(
            "workflow_run",
            payload,
            greeting="今天流水线很顺利，记得歇口气。",
        )
        self.assertIn("今天流水线很顺利，记得歇口气。", card["card"]["elements"][0]["text"]["content"])

    def test_summarize_ci_greeting_uses_failure_fallback_without_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}, clear=False):
            self.assertEqual(
                notify.summarize_ci_greeting("ok", "1s", "dev", "failure"),
                "流水线执行失败，请点击 Open run 查看失败步骤。",
            )

    def test_summarize_ci_greeting_retries_transient_http_error_then_falls_back(self):
        def boom(*args, **kwargs):
            raise HTTPError("https://api.deepseek.com", 500, "boom", hdrs=None, fp=BytesIO(b"err"))

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            with patch.object(notify, "urlopen", boom):
                with patch.object(notify.time, "sleep") as sleep:
                    self.assertEqual(
                        notify.summarize_ci_greeting("ok", "1s", "dev", "success"),
                        "流水线已成功完成，辛苦了，记得稍作休息。",
                    )
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_summarize_ci_greeting_retries_empty_content_then_returns_text(self):
        class _Response:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": self.content}}]}
                ).encode()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            with patch.object(
                notify,
                "urlopen",
                side_effect=[_Response(""), _Response("今天构建通过，记得休息。")],
            ) as urlopen:
                with patch.object(notify.time, "sleep") as sleep:
                    result = notify.summarize_ci_greeting("ok", "1s", "dev", "success")
        self.assertEqual(result, "今天构建通过，记得休息。")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)
        requests = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        self.assertEqual([request["max_tokens"] for request in requests], [1024, 2048])

    def test_summarize_ci_greeting_does_not_retry_non_retryable_http_error(self):
        def unauthorized(*args, **kwargs):
            raise HTTPError("https://api.deepseek.com", 401, "unauthorized", hdrs=None, fp=BytesIO(b"err"))

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False):
            with patch.object(notify, "urlopen", side_effect=unauthorized) as urlopen:
                with patch.object(notify.time, "sleep") as sleep:
                    result = notify.summarize_ci_greeting("ok", "1s", "dev", "cancelled")
        self.assertEqual(result, "流水线已取消，请点击 Open run 查看执行记录。")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_previous_release_tag_skips_the_current_tag(self):
        releases = [
            {"tag_name": "v1.2.3", "draft": False},
            {"tag_name": "v1.2.2", "draft": False},
            {"tag_name": "v1.2.1", "draft": True},
            {"tag_name": "v1.2.0", "draft": False},
        ]
        self.assertEqual(notify.previous_release_tag(releases, "v1.2.3"), "v1.2.2")
        self.assertIsNone(notify.previous_release_tag([{"tag_name": "v1.0.0", "draft": False}], "v1.0.0"))


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
