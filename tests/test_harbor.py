import json
import unittest
from io import BytesIO, StringIO
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from ci_templates.harbor import (
    HarborClient,
    HarborDigestConflict,
    HarborError,
    HarborHTTPError,
    HarborTransportError,
    ImageRef,
)


EXPECTED = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
MISSING = (404, {}, b"")


class _FakeResponse:
    def __init__(self, digest=EXPECTED, status=200):
        self.status = status
        self.headers = {"Docker-Content-Digest": digest} if digest else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


def _manifest(digest):
    return 200, {"Docker-Content-Digest": digest}, b""


def _client():
    return HarborClient("harbor.example.local", username="robot", password="secret")


def _image(tag="sha-example"):
    return ImageRef.parse(f"harbor.example.local/knowledge-core/web:{tag}")


class HarborRequestStatusTest(unittest.TestCase):
    @patch("ci_templates.harbor.urlopen")
    def test_explicitly_accepted_http_error_returns_status_and_closes_body(self, urlopen):
        error = HTTPError(
            "https://harbor.example.local/api",
            409,
            "Conflict",
            {},
            BytesIO(b"already exists"),
        )
        urlopen.side_effect = error

        status, _headers, payload = _client()._request(
            "POST", "/api", body={"name": "tag"}, accepted_statuses={409}
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload, b"already exists")
        self.assertTrue(error.fp.closed)

    @patch("ci_templates.harbor.urlopen")
    def test_unexpected_http_error_has_structured_status(self, urlopen):
        urlopen.side_effect = HTTPError(
            "https://harbor.example.local/api", 404, "Not Found", {}, BytesIO()
        )

        with self.assertRaises(HarborHTTPError) as raised:
            _client()._request("GET", "/api")

        self.assertEqual(raised.exception.status_code, 404)


class HarborManifestDigestTest(unittest.TestCase):
    @patch("ci_templates.harbor.urlopen", return_value=_FakeResponse())
    def test_manifest_digest_accepts_index_and_list_media_types(self, urlopen):
        client = _client()
        digest = client.manifest_digest(_image("dev"))

        self.assertEqual(digest, EXPECTED)
        request = urlopen.call_args.args[0]
        accept = request.get_header("Accept")
        for media_type in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ):
            self.assertIn(media_type, accept)

    def test_missing_tag_uses_structured_404(self):
        client = _client()
        with patch.object(client, "_request", return_value=MISSING) as request:
            self.assertIsNone(client.manifest_digest(_image()))
        self.assertEqual(request.call_args.kwargs["accepted_statuses"], {404})

    def test_success_without_digest_is_not_treated_as_missing(self):
        client = _client()
        with patch.object(client, "_request", return_value=(200, {}, b"")):
            with self.assertRaisesRegex(HarborError, "omitted digest"):
                client.manifest_digest(_image())


class HarborTagRestoreTest(unittest.TestCase):
    def test_existing_matching_tag_is_idempotent_without_post(self):
        client = _client()
        with patch.object(client, "_request", return_value=_manifest(EXPECTED)) as request:
            result = client.tag_digest(_image(), EXPECTED)

        self.assertEqual(result, "already-present")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "HEAD")

    def test_existing_mismatched_tag_is_rejected_without_post(self):
        client = _client()
        with patch.object(client, "_request", return_value=_manifest(OTHER)) as request:
            with self.assertRaises(HarborDigestConflict) as raised:
                client.tag_digest(_image(), EXPECTED)

        self.assertEqual(raised.exception.expected, EXPECTED)
        self.assertEqual(raised.exception.actual, OTHER)
        self.assertEqual(request.call_count, 1)

    def test_missing_tag_is_created_and_verified(self):
        client = _client()
        with patch.object(
            client, "_request", side_effect=[MISSING, (201, {}, b""), _manifest(EXPECTED)]
        ) as request:
            result = client.tag_digest(_image(), EXPECTED)

        self.assertEqual(result, "created")
        self.assertEqual(request.call_count, 3)
        post = request.call_args_list[1]
        self.assertEqual(post.args[0], "POST")
        self.assertEqual(post.kwargs["accepted_statuses"], {409})

    def test_conflict_is_reconciled_when_tag_now_has_expected_digest(self):
        client = _client()
        with patch.object(client, "_request", side_effect=[MISSING, (409, {}, b""), _manifest(EXPECTED)]):
            self.assertEqual(client.tag_digest(_image(), EXPECTED), "reconciled-after-conflict")

    def test_conflict_with_different_digest_fails(self):
        client = _client()
        with patch.object(client, "_request", side_effect=[MISSING, (409, {}, b""), _manifest(OTHER)]):
            with self.assertRaises(HarborDigestConflict):
                client.tag_digest(_image(), EXPECTED)

    def test_conflict_with_still_missing_tag_preserves_409(self):
        client = _client()
        with patch.object(client, "_request", side_effect=[MISSING, (409, {}, b""), MISSING]):
            with self.assertRaises(HarborHTTPError) as raised:
                client.tag_digest(_image(), EXPECTED)
        self.assertEqual(raised.exception.status_code, 409)

    def test_transport_failure_is_reconciled_if_write_succeeded(self):
        client = _client()
        failure = HarborTransportError("connection reset")
        with patch.object(client, "_request", side_effect=[MISSING, failure, _manifest(EXPECTED)]):
            self.assertEqual(client.tag_digest(_image(), EXPECTED), "reconciled-after-conflict")

    def test_transport_failure_is_preserved_if_tag_remains_missing(self):
        client = _client()
        failure = HarborTransportError("connection reset")
        with patch.object(client, "_request", side_effect=[MISSING, failure, MISSING]):
            with self.assertRaises(HarborTransportError) as raised:
                client.tag_digest(_image(), EXPECTED)
        self.assertIs(raised.exception, failure)

    def test_server_error_is_reconciled_without_retrying_the_write(self):
        client = _client()
        failure = HarborHTTPError("POST", "/tags", 500)
        with patch.object(client, "_request", side_effect=[MISSING, failure, _manifest(EXPECTED)]) as request:
            self.assertEqual(client.tag_digest(_image(), EXPECTED), "reconciled-after-conflict")
        self.assertEqual(request.call_count, 3)


class HarborDeleteAndPromotionTest(unittest.TestCase):
    def test_delete_race_404_is_success_when_tag_is_gone(self):
        client = _client()
        with patch.object(client, "_request", side_effect=[_manifest(EXPECTED), (404, {}, b""), MISSING]):
            self.assertEqual(client.delete_tag(_image()), "already-absent")

    def test_delete_transport_failure_is_success_when_postcondition_holds(self):
        client = _client()
        failure = HarborTransportError("connection reset")
        with patch.object(client, "_request", side_effect=[_manifest(EXPECTED), failure, MISSING]):
            self.assertEqual(client.delete_tag(_image()), "reconciled-after-delete")

    def test_delete_expected_digest_prevents_deleting_a_concurrent_retag(self):
        client = _client()
        with patch.object(client, "_request", return_value=_manifest(OTHER)) as request:
            self.assertEqual(
                client.delete_tag(_image(), expected_digest=EXPECTED), "changed"
            )
        self.assertEqual(request.call_count, 1)

    def test_promotion_accepts_a_concurrent_success(self):
        client = _client()
        with (
            patch.object(client, "manifest_digest", side_effect=[EXPECTED, OTHER, EXPECTED]),
            patch.object(client, "delete_tag", return_value="deleted") as delete_tag,
            patch.object(client, "tag_digest") as tag_digest,
        ):
            self.assertEqual(client.promote_tag(_image("sha-candidate"), _image("dev")), EXPECTED)
        delete_tag.assert_called_once_with(_image("dev"), expected_digest=OTHER)
        tag_digest.assert_not_called()

    def test_promotion_does_not_overwrite_unexpected_concurrent_digest(self):
        client = _client()
        with (
            patch.object(client, "manifest_digest", side_effect=[EXPECTED, OTHER, "sha256:" + "c" * 64]),
            patch.object(client, "delete_tag", return_value="deleted"),
            patch.object(client, "tag_digest") as tag_digest,
        ):
            with self.assertRaises(HarborDigestConflict):
                client.promote_tag(_image("sha-candidate"), _image("dev"))
        tag_digest.assert_not_called()

    def test_promotion_restores_previous_digest_only_when_destination_is_missing(self):
        client = _client()
        operation_failed = HarborError("promotion request failed")
        with (
            patch.object(client, "manifest_digest", side_effect=[EXPECTED, OTHER, None, None, OTHER]),
            patch.object(client, "delete_tag", return_value="deleted"),
            patch.object(client, "tag_digest", side_effect=[operation_failed, "created"]) as tag_digest,
        ):
            with self.assertRaises(HarborError) as raised:
                client.promote_tag(_image("sha-candidate"), _image("dev"))
        self.assertIs(raised.exception, operation_failed)
        self.assertEqual(tag_digest.call_count, 2)
        self.assertEqual(tag_digest.call_args_list[1].args, (_image("dev"), OTHER))

    def test_promotion_reports_failed_compensation(self):
        client = _client()
        operation_failed = HarborError("promotion request failed")
        restore_failed = HarborError("old digest no longer available")
        with (
            patch.object(client, "manifest_digest", side_effect=[EXPECTED, OTHER, None, None]),
            patch.object(client, "delete_tag", return_value="deleted"),
            patch.object(client, "tag_digest", side_effect=[operation_failed, restore_failed]),
        ):
            with self.assertRaisesRegex(HarborError, "restoring .* failed") as raised:
                client.promote_tag(_image("sha-candidate"), _image("dev"))
        self.assertIs(raised.exception.__cause__, restore_failed)


class HarborRestoreCliTest(unittest.TestCase):
    @patch("ci_templates.__main__.HarborClient")
    @patch("ci_templates.__main__.load_config")
    def test_restore_cli_reuses_client_and_preserves_json_fields(self, load_config, harbor_cls):
        from ci_templates.__main__ import main

        service = SimpleNamespace(
            name="web", image_repository="harbor.example.local/knowledge-core/web"
        )
        load_config.return_value = SimpleNamespace(
            services=[service], harbor_registry="harbor.example.local"
        )
        client = harbor_cls.return_value
        client.tag_digest.return_value = "already-present"
        stdout = StringIO()
        stderr = StringIO()

        with (
            patch("ci_templates.__main__.sys.stdout", stdout),
            patch("ci_templates.__main__.sys.stderr", stderr),
        ):
            self.assertEqual(
                main([
                    "restore-candidate",
                    "--config",
                    ".ci/pipeline.yaml",
                    "--service",
                    "web",
                    "--tag",
                    "sha-example",
                    "--digest",
                    EXPECTED,
                ]),
                0,
            )

        harbor_cls.assert_called_once_with("harbor.example.local")
        client.tag_digest.assert_called_once()
        client.manifest_digest.assert_not_called()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"service": "web", "tag": "sha-example", "digest": EXPECTED},
        )
        self.assertIn("candidate tag restore: already-present", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
