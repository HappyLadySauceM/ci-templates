import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from ci_templates.release import ReleaseError, summarize_release_with_deepseek, summarize_with_deepseek


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class DeepSeekReleaseTest(unittest.TestCase):
    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False)
    @patch("ci_templates.release.urlopen")
    def test_empty_response_retries_with_larger_output_budget(self, urlopen):
        urlopen.side_effect = [
            _Response({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}),
            _Response({"choices": [{"finish_reason": "stop", "message": {"content": "- Added sharing controls"}}]}),
        ]

        summary = summarize_with_deepseek("deepseek-test", "gateway", "gateway-v1.2.3", {"paths": [], "diff": ""})

        self.assertEqual(summary, "- Added sharing controls")
        self.assertEqual(urlopen.call_count, 2)
        requests = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        self.assertEqual([request["max_tokens"] for request in requests], [4096, 8192])
        self.assertTrue(all(call.kwargs["timeout"] == 120 for call in urlopen.call_args_list))

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False)
    @patch("ci_templates.release.time.sleep")
    @patch("ci_templates.release.urlopen")
    def test_aggregate_summary_retries_transient_http_failure(self, urlopen, sleep):
        urlopen.side_effect = [
            HTTPError("https://api.deepseek.com", 503, "busy", {}, None),
            _Response({"choices": [{"message": {"content": "- remote summary"}}]}),
        ]

        body = summarize_release_with_deepseek(
            "deepseek-test", "v1.2.3", {"shared": {}, "services": {}}, ["gateway"], "zh-CN"
        )

        self.assertIn("- remote summary", body)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}, clear=False)
    @patch("ci_templates.release.time.sleep")
    @patch("ci_templates.release.urlopen")
    def test_non_retryable_http_failure_fails_without_local_summary(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("https://api.deepseek.com", 401, "unauthorized", {}, None)

        with self.assertRaisesRegex(ReleaseError, "HTTP 401"):
            summarize_release_with_deepseek(
                "deepseek-test", "v1.2.3", {"shared": {}, "services": {}}, ["gateway"], "zh-CN"
            )

        urlopen.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
