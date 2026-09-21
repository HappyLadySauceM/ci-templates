import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from zipfile import ZipFile

from ci_templates.artifact_cache import ArtifactCacheError, restore
from ci_templates.cache_maintenance import prune_cache
from ci_templates.maintenance import MaintenanceError, prune_candidates
from ci_templates.transport import RetryPolicy, backoff_seconds, request_with_retry
from test_ci_templates import config


class RetryTest(unittest.TestCase):
    def test_read_retries_transient_http_and_uses_retry_after(self):
        request = Request("https://example.test/resource")
        calls = []

        def opener(_request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    429,
                    "busy",
                    {"Retry-After": "3"},
                    None,
                )
            return object()

        sleeps = []
        result = request_with_retry(
            opener,
            request,
            timeout=10,
            policy=RetryPolicy(attempts=2, jitter=0),
            sleep=sleeps.append,
        )
        self.assertIsNotNone(result)
        self.assertEqual(sleeps, [3])
        self.assertEqual(len(calls), 2)

    def test_writes_are_not_retried_without_opt_in(self):
        request = Request("https://example.test/resource", data=b"{}", method="POST")
        opener = lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPError(request.full_url, 503, "busy", {}, None))
        with self.assertRaises(HTTPError):
            request_with_retry(opener, request, timeout=10, policy=RetryPolicy(attempts=4, jitter=0), sleep=lambda _: self.fail("slept"))

    def test_backoff_is_bounded(self):
        self.assertEqual(backoff_seconds(RetryPolicy(initial_delay=2, maximum_delay=3, jitter=0), 5), 3)


class ArtifactCacheTest(unittest.TestCase):
    def test_restore_uses_remote_zip_then_cache_without_second_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            environment = {
                "CI_ARTIFACT_CACHE_ROOT": str(root),
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_RUN_ID": "42",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_TOKEN": "token",
            }
            archive = Path(directory) / "artifact.zip"
            with ZipFile(archive, "w") as stream:
                stream.writestr("changes.json", "{}\n")

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return archive.read_bytes()

            payload = {"artifacts": [{"id": 1, "name": "plan", "expired": False, "archive_download_url": "https://example.test/archive"}]}
            with patch.dict(os.environ, environment, clear=False):
                with patch("ci_templates.artifact_cache._request", return_value=payload), patch("ci_templates.artifact_cache.urlopen", return_value=Response()):
                    destination = Path(directory) / "first"
                    result = restore("plan", str(destination))
                self.assertEqual(result["source"], "github")
                self.assertEqual((destination / "changes.json").read_text(), "{}\n")
                with patch("ci_templates.artifact_cache._request", side_effect=AssertionError("remote should not be used")):
                    result = restore("plan", str(Path(directory) / "second"))
                self.assertEqual(result["source"], "cache")

    def test_restore_rejects_unsafe_cached_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            environment = {
                "CI_ARTIFACT_CACHE_ROOT": str(root),
                "GITHUB_REPOSITORY": "org/repo",
                "GITHUB_RUN_ID": "42",
                "GITHUB_SHA": "a" * 40,
            }
            entry = root / "42" / ("a" * 40) / "plan"
            entry.mkdir(parents=True)
            payload = entry / "payload.zip"
            with ZipFile(payload, "w") as stream:
                stream.writestr("../escape", "bad")
            (entry / "metadata.json").write_text(json.dumps({"repository": "org/repo", "run_id": "42", "sha": "a" * 40, "sha256": __import__("hashlib").sha256(payload.read_bytes()).hexdigest()}))
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(ArtifactCacheError):
                    restore("plan", str(Path(directory) / "destination"))
            self.assertFalse(entry.exists())


class MaintenanceTest(unittest.TestCase):
    def test_pruning_fails_closed_when_active_run_lookup_fails(self):
        with patch("ci_templates.maintenance._active_commit_shas", side_effect=MaintenanceError("unavailable")):
            with self.assertRaises(MaintenanceError):
                prune_candidates(config(), harbor_factory=lambda _: self.fail("harbor must not be touched"))

    @patch("ci_templates.cache_maintenance.os.statvfs")
    def test_cache_pressure_evicts_oldest_dependency_entries_to_low_watermark(self, statvfs):
        statvfs.return_value = type("Stats", (), {"f_blocks": 100, "f_bavail": 10, "f_frsize": 1})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            newest = root / "npm" / "newest.tgz"
            oldest = root / "npm" / "oldest.tgz"
            newest.parent.mkdir(parents=True)
            newest.write_bytes(b"new")
            oldest.write_bytes(b"old")
            os.utime(oldest, (98, 98))
            os.utime(newest, (99, 99))
            removed = prune_cache(str(root), dry_run=True, now=100)
            self.assertIn("npm/oldest.tgz", removed)
            self.assertIn("npm/newest.tgz", removed)


if __name__ == "__main__":
    unittest.main()
