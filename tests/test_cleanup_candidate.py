import os
import unittest
from io import StringIO
from unittest.mock import patch

from ci_templates.github import GitHubError
from test_ci_templates import config


class CleanupCandidateAttemptTest(unittest.TestCase):
    def _run(self):
        from ci_templates.__main__ import main

        return main(["cleanup-candidate", "--service", "gateway", "--tag", "sha-abc"])

    def _github_env(self, attempt: str) -> dict[str, str]:
        return {
            "GITHUB_TOKEN": "token",
            "GITHUB_REPOSITORY": "org/example",
            "GITHUB_RUN_ID": "99",
            "GITHUB_RUN_ATTEMPT": attempt,
        }

    @patch("ci_templates.github._request", return_value={"run_attempt": 2})
    @patch("ci_templates.__main__.HarborClient")
    @patch("ci_templates.__main__.load_config")
    def test_cleanup_candidate_deletes_when_this_attempt_is_latest(self, load_config, harbor_cls, _request):
        load_config.return_value = config()
        client = harbor_cls.return_value
        client.delete_tag.return_value = "deleted"

        with patch.dict(os.environ, self._github_env("2"), clear=False):
            self.assertEqual(self._run(), 0)

        client.delete_tag.assert_called_once()
        image = client.delete_tag.call_args.args[0]
        self.assertEqual(image.tag_ref, "org/gateway:sha-abc")

    @patch("ci_templates.github._request", return_value={"run_attempt": 3})
    @patch("ci_templates.__main__.HarborClient")
    @patch("ci_templates.__main__.load_config")
    def test_cleanup_candidate_skips_delete_when_attempt_is_stale(self, load_config, harbor_cls, _request):
        load_config.return_value = config()
        stderr = StringIO()

        with (
            patch.dict(os.environ, self._github_env("1"), clear=False),
            patch("ci_templates.__main__.sys.stderr", stderr),
        ):
            self.assertEqual(self._run(), 0)

        harbor_cls.assert_not_called()
        self.assertIn("run attempt 1 is stale; latest is 3", stderr.getvalue())

    @patch("ci_templates.__main__.HarborClient")
    @patch("ci_templates.__main__.load_config")
    def test_cleanup_candidate_deletes_when_github_run_env_is_absent(self, load_config, harbor_cls):
        load_config.return_value = config()
        client = harbor_cls.return_value
        client.delete_tag.return_value = "deleted"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY"}
        }

        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._run(), 0)

        client.delete_tag.assert_called_once()

    @patch("ci_templates.github._request", side_effect=GitHubError("GitHub API GET /runs/99 failed: HTTP 500"))
    @patch("ci_templates.__main__.HarborClient")
    @patch("ci_templates.__main__.load_config")
    def test_cleanup_candidate_skips_delete_when_run_lookup_fails(self, load_config, harbor_cls, _request):
        load_config.return_value = config()
        stderr = StringIO()

        with (
            patch.dict(os.environ, self._github_env("1"), clear=False),
            patch("ci_templates.__main__.sys.stderr", stderr),
        ):
            self.assertEqual(self._run(), 0)

        harbor_cls.assert_not_called()
        self.assertIn("GitHub run lookup failed", stderr.getvalue())
