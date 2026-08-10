import json
import os
import unittest
from unittest.mock import patch

from ci_templates.release import summarize_with_deepseek


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


if __name__ == "__main__":
    unittest.main()
