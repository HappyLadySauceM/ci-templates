import unittest
from unittest.mock import patch

from ci_templates.harbor import HarborClient, ImageRef


class _FakeResponse:
    def __init__(self, digest="sha256:" + "a" * 64):
        self.status = 200
        self.headers = {"Docker-Content-Digest": digest}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


class HarborManifestDigestTest(unittest.TestCase):
    @patch("ci_templates.harbor.urlopen", return_value=_FakeResponse())
    def test_manifest_digest_accepts_index_and_list_media_types(self, urlopen):
        # Harbor :dev tags are often a manifest list/index. A single OCI
        # manifest Accept makes HEAD return 404, so promote skips delete and
        # POST /tags then 409s.
        # Harbor 的 :dev 经常是 manifest list/index。只接受单层 OCI manifest
        # 时 HEAD 会 404，promote 跳过删除后 POST /tags 就会 409。
        client = HarborClient("harbor.example.local", username="robot", password="secret")
        digest = client.manifest_digest(ImageRef.parse("harbor.example.local/knowledge-core/web:dev"))

        self.assertTrue(digest.startswith("sha256:"))
        request = urlopen.call_args.args[0]
        accept = request.get_header("Accept")
        for media_type in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ):
            self.assertIn(media_type, accept)
