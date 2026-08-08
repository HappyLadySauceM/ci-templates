import json
from pathlib import Path
import hashlib
import io
import tarfile
import subprocess
from unittest.mock import call, patch

import unittest

from ci_templates.changes import affected_services
from ci_templates.config import ConfigError, Pipeline
from ci_templates.gitops import update_images
from ci_templates.versions import service_tag
from ci_templates.charts import Chart, ChartError, _extract_chart, _relative_path, _validate_rendered, load_chart_manifest
from ci_templates.build import build_service


def config() -> Pipeline:
    return Pipeline.from_mapping({
        "project": "example",
        "source_repo": "org/example",
        "gitops_repo": "org/gitops",
        "gitops_path": "Example",
        "gitops_branch": "main",
        "shared_paths": ["pkg", "idl"],
        "base_images": [{"source": "example/base:source", "destination": "example/base:cached"}],
        "services": [{
            "name": "gateway", "source_path": "services/gateway", "version_file": "services/gateway/VERSION",
            "dockerfile": "services/gateway/Dockerfile", "context": ".", "image_repository": "org/gateway",
            "deploy_snapshot": "deploy/gateway",
        }],
    })


class CiTemplatesTest(unittest.TestCase):
    def test_config_requires_services(self):
        with self.assertRaises(ConfigError):
            Pipeline.from_mapping({"project": "example"})


    def test_shared_changes_rebuild_service(self):
        self.assertEqual(affected_services(config(), ["pkg/auth/token.go"]), ("gateway",))


    def test_unrelated_changes_are_ignored(self):
        self.assertEqual(affected_services(config(), ["docs/README.md"]), ())


    def test_service_tag(self):
        self.assertEqual(service_tag("gateway", (1, 2, 3)), "gateway-v1.2.3")


    def test_update_images(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kustomization.yaml"
            path.write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nimages:\n- name: old\n  newTag: old\n", encoding="utf-8")
            update_images(path, {"old": {"newTag": "dev", "digest": "sha256:abc"}})
            text = path.read_text(encoding="utf-8")
            self.assertIn("digest: sha256:abc", text)
            self.assertNotIn("newTag: old", text)

    @patch("ci_templates.build._docker")
    def test_build_preserves_existing_dev_before_replacement(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]

        build_service(config().services[0])

        self.assertEqual(
            docker.call_args_list[:3],
            [
                call(["pull", "org/gateway:dev"], cwd=".", check=False),
                call(["tag", "org/gateway:dev", "org/gateway:previous"], cwd="."),
                call(["push", "org/gateway:previous"], cwd="."),
            ],
        )

    @patch("ci_templates.build._docker")
    def test_first_build_does_not_require_previous_image(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        ]

        build_service(config().services[0])

        self.assertEqual(docker.call_count, 2)
        self.assertEqual(docker.call_args_list[1].args[0][:3], ["buildx", "build", "--push"])

    def test_chart_manifest_rejects_invalid_digest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "charts.yaml"
            path.write_text("""version: 1
destination: oci://registry.example/charts
charts:
- name: example
  repository: https://charts.example
  version: 1.0.0
  sha256: invalid
  targetVersion: 1.0.0
  releaseName: example
  namespace: example
""", encoding="utf-8")
            with self.assertRaises(ChartError):
                load_chart_manifest(path)

    def test_chart_paths_cannot_escape_repository(self):
        with self.assertRaises(ChartError):
            _relative_path("../secret.yaml", "test")

    def test_chart_render_rejects_secret(self):
        chart = Chart("example", "https://charts.example", "1", "a" * 64, "1", "example", "default", (), False, (), (), None)
        rendered = b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: forbidden\n"
        with self.assertRaisesRegex(ChartError, "forbidden Secret"):
            _validate_rendered(chart, rendered)

    def test_chart_render_rejects_duplicate_resource(self):
        chart = Chart("example", "https://charts.example", "1", "a" * 64, "1", "example", "default", (), False, (), (), None)
        resource = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: duplicate\n"
        with self.assertRaisesRegex(ChartError, "duplicate resources"):
            _validate_rendered(chart, (resource + "---\n" + resource).encode())

    def test_chart_archive_rejects_path_traversal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "chart.tgz"
            with tarfile.open(archive, "w:gz") as bundle:
                content = b"unsafe"
                member = tarfile.TarInfo("../unsafe")
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))
            with self.assertRaisesRegex(ChartError, "unsafe path"):
                _extract_chart(archive, Path(directory) / "extract")
