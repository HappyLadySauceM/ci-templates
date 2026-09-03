import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ACTION_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions" / "feishu-pipeline-task"
SPEC = importlib.util.spec_from_file_location("feishu_pipeline_task", ACTION_DIR / "task_tracker.py")
task_tracker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = task_tracker
SPEC.loader.exec_module(task_tracker)


def workflow_run(**overrides):
    run = {
        "id": 200,
        "run_attempt": 1,
        "run_number": 20,
        "workflow_id": 10,
        "name": "knowledge-core-pipeline",
        "display_title": "fix(ci): make artifacts reusable across reruns",
        "html_url": "https://github.com/org/repo/actions/runs/200",
        "event": "push",
        "head_branch": "dev",
        "head_sha": "new",
        "created_at": "2026-09-03T01:00:00Z",
        "run_started_at": "2026-09-03T01:00:05Z",
        "updated_at": "2026-09-03T01:02:00Z",
        "conclusion": "success",
        "actor": {"login": "alice"},
    }
    run.update(overrides)
    return run


class NormalizeTest(unittest.TestCase):
    def test_normalizes_supported_github_identity_formats(self):
        self.assertEqual(task_tracker.normalize_github_login("Alice"), "alice")
        self.assertEqual(task_tracker.normalize_github_login("@Alice"), "alice")
        self.assertEqual(
            task_tracker.normalize_github_login("https://github.com/Alice/"),
            "alice",
        )
        self.assertEqual(task_tracker.normalize_github_login("two words"), "")


class StateTest(unittest.TestCase):
    def test_maps_workflow_run_states(self):
        self.assertEqual(task_tracker.board_state("requested", ""), "执行中")
        self.assertEqual(task_tracker.board_state("in_progress", ""), "执行中")
        self.assertEqual(task_tracker.board_state("completed", "success"), "执行完毕")
        self.assertEqual(task_tracker.board_state("completed", "skipped"), "执行完毕")
        self.assertEqual(task_tracker.board_state("completed", "failure"), "执行出错")
        self.assertEqual(task_tracker.board_state("completed", "cancelled"), "执行出错")

    def test_rejects_unknown_terminal_conclusion(self):
        with self.assertRaises(task_tracker.TrackerError):
            task_tracker.board_state("completed", "mystery")

    def test_ignores_older_runs_and_late_in_progress_events(self):
        extra = {
            "latest": {
                "run_id": 200,
                "run_attempt": 2,
                "phase": 2,
                "state": "执行完毕",
            }
        }
        self.assertTrue(task_tracker.is_stale_or_duplicate(extra, (199, 9, 2), "执行出错"))
        self.assertTrue(task_tracker.is_stale_or_duplicate(extra, (200, 2, 1), "执行中"))
        self.assertTrue(task_tracker.is_stale_or_duplicate(extra, (200, 2, 2), "执行完毕"))
        self.assertFalse(task_tracker.is_stale_or_duplicate(extra, (200, 3, 1), "执行中"))


class ContributorTest(unittest.TestCase):
    def test_push_compares_with_previous_distinct_run(self):
        github = MagicMock()
        github.get.side_effect = [
            {
                "workflow_runs": [
                    {"id": 199, "run_number": 19, "head_sha": "old"},
                    {"id": 198, "run_number": 18, "head_sha": "older"},
                ]
            },
            {
                "commits": [
                    {"author": {"login": "Alice"}, "committer": {"login": "github-actions[bot]"}},
                    {"author": {"login": "Bob"}, "committer": None},
                ]
            },
        ]
        self.assertEqual(
            task_tracker.contributor_logins(github, "org/repo", workflow_run()),
            {"alice", "bob"},
        )
        self.assertIn("/compare/old...new", github.get.call_args_list[1].args[0])

    def test_manual_run_has_no_commit_contributors(self):
        github = MagicMock()
        self.assertEqual(
            task_tracker.contributor_logins(
                github, "org/repo", workflow_run(event="workflow_dispatch")
            ),
            set(),
        )
        github.get.assert_not_called()


class IdentityTest(unittest.TestCase):
    def test_maps_only_group_members_and_skips_duplicate_logins(self):
        feishu = MagicMock()
        feishu.pages.side_effect = [
            [{"id": "github-field", "type": "TEXT"}],
            [
                {"member_id": "ou-alice"},
                {"member_id": "ou-duplicate-1"},
                {"member_id": "ou-duplicate-2"},
            ],
        ]
        feishu.call.return_value = {
            "items": [
                {
                    "open_id": "ou-alice",
                    "custom_attrs": [
                        {"id": "github-field", "value": {"text": "https://github.com/Alice"}}
                    ],
                },
                {
                    "open_id": "ou-duplicate-1",
                    "custom_attrs": [{"id": "github-field", "value": {"text": "bob"}}],
                },
                {
                    "open_id": "ou-duplicate-2",
                    "custom_attrs": [{"id": "github-field", "value": {"text": "@Bob"}}],
                },
            ]
        }
        self.assertEqual(
            task_tracker.group_identity_map(feishu, "oc-chat", "github-field"),
            {"alice": "ou-alice"},
        )


class TrackerTest(unittest.TestCase):
    def make_tracker(self):
        return task_tracker.PipelineTracker(
            MagicMock(),
            MagicMock(),
            repository="org/repo",
            workflow_name="knowledge-core-pipeline",
            chat_id="oc-chat",
            attr_id="github-field",
            tasklist_name="CICD 流水线",
        )

    def test_provision_creates_untriggered_task_once(self):
        tracker = self.make_tracker()
        tracker.ensure_board = MagicMock(
            return_value=({"guid": "list"}, {name: "section-%s" % name for name in task_tracker.BOARD_STATES})
        )
        tracker.find_task = MagicMock(return_value=(None, {}))
        tracker.create_task = MagicMock(return_value={"guid": "task"})

        result = tracker.provision()

        self.assertEqual(result["state"], "未触发")
        self.assertEqual(result["task_guid"], "task")
        self.assertEqual(tracker.create_task.call_args.args[1], "section-未触发")

    def test_sync_updates_section_and_only_managed_followers(self):
        tracker = self.make_tracker()
        tracker.ensure_board = MagicMock(
            return_value=({"guid": "list"}, {name: "section-%s" % name for name in task_tracker.BOARD_STATES})
        )
        old_extra = {
            "managed_followers": ["ou-old"],
            "latest": {"run_id": 199, "run_attempt": 1, "phase": 2, "state": "执行出错"},
        }
        tracker.find_task = MagicMock(
            return_value=(
                {
                    "guid": "task",
                    "members": [
                        {"id": "ou-old", "type": "user", "role": "follower"},
                        {"id": "ou-manual", "type": "user", "role": "follower"},
                    ],
                },
                old_extra,
            )
        )
        with patch.object(task_tracker, "group_identity_map", return_value={"alice": "ou-new"}):
            with patch.object(task_tracker, "contributor_logins", return_value={"alice"}):
                with patch.object(task_tracker, "summarize_ci_greeting", return_value="辛苦了"):
                    result = tracker.sync(workflow_run(), "completed")

        self.assertEqual(result["state"], "执行完毕")
        paths = [call.args[1] for call in tracker.feishu.call.call_args_list]
        self.assertIn("/open-apis/task/v2/tasks/task/add_tasklist", paths)
        self.assertIn("/open-apis/task/v2/tasks/task/remove_members", paths)
        self.assertIn("/open-apis/task/v2/tasks/task/add_members", paths)
        patch_call = next(
            call
            for call in tracker.feishu.call.call_args_list
            if call.args[0] == "PATCH"
        )
        serialized = json.loads(patch_call.kwargs["body"]["task"]["extra"])
        self.assertEqual(serialized["managed_followers"], ["ou-new"])
        self.assertNotIn("ou-manual", serialized["managed_followers"])

    def test_stale_event_does_not_call_identity_or_update_apis(self):
        tracker = self.make_tracker()
        tracker.ensure_board = MagicMock(
            return_value=({"guid": "list"}, {name: "section-%s" % name for name in task_tracker.BOARD_STATES})
        )
        tracker.find_task = MagicMock(
            return_value=(
                {"guid": "task"},
                {
                    "managed_followers": ["ou-alice"],
                    "latest": {"run_id": 300, "run_attempt": 1, "phase": 2, "state": "执行完毕"},
                },
            )
        )
        with patch.object(task_tracker, "group_identity_map") as identity:
            result = tracker.sync(workflow_run(), "completed")
        self.assertEqual(result["state"], "执行完毕")
        identity.assert_not_called()
        tracker.feishu.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
