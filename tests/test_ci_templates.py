import json
from pathlib import Path

import unittest

from ci_templates.changes import affected_services
from ci_templates.config import ConfigError, Pipeline
from ci_templates.gitops import update_images
from ci_templates.versions import service_tag


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
