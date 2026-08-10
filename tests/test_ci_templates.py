import json
from pathlib import Path
import hashlib
import io
import tarfile
import subprocess
import sys
from unittest.mock import call, patch

import unittest

from ci_templates.changes import affected_services
from ci_templates.config import ConfigError, Pipeline
from ci_templates.gitops import _git, promote_snapshot, rollback_snapshot, update_images
from ci_templates.versions import service_tag
from ci_templates.charts import Chart, ChartError, _extract_chart, _relative_path, _validate_rendered, load_chart_manifest
from ci_templates.build import BuildError, _docker, build_service, image_digest
from ci_templates.argocd import _has_revision


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

    @patch("ci_templates.build.subprocess.run")
    def test_docker_progress_is_written_to_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)

        _docker(["pull", "org/gateway:dev"])

        self.assertIs(run.call_args.kwargs["stdout"], sys.stderr)

    @patch("ci_templates.gitops.subprocess.run")
    def test_git_progress_is_written_to_stderr(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)

        _git(["status"])

        self.assertIs(run.call_args.kwargs["stdout"], sys.stderr)


    def test_shared_changes_rebuild_service(self):
        self.assertEqual(affected_services(config(), ["pkg/auth/token.go"]), ("gateway",))


    def test_unrelated_changes_are_ignored(self):
        self.assertEqual(affected_services(config(), ["docs/README.md"]), ())


    def test_service_tag(self):
        self.assertEqual(service_tag("gateway", (1, 2, 3)), "gateway-v1.2.3")

    def test_argo_revision_supports_single_and_multi_source_applications(self):
        revision = "a" * 40
        self.assertTrue(_has_revision({"revision": revision}, revision))
        self.assertTrue(_has_revision({"revisions": [revision, revision]}, revision))
        self.assertFalse(_has_revision({"revisions": ["b" * 40]}, revision))


    def test_update_images(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kustomization.yaml"
            path.write_text("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nimages:\n- name: old\n  newTag: old\n", encoding="utf-8")
            update_images(path, {"old": {"newTag": "dev", "digest": "sha256:abc"}})
            text = path.read_text(encoding="utf-8")
            self.assertIn("digest: sha256:abc", text)
            self.assertNotIn("newTag: old", text)

    def test_promote_snapshot_commits_image_digest(self):
        import tempfile

        def git(*args, cwd=None):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "deployment.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
            remote = root / "remote.git"
            seed = root / "seed"
            git("init", "--bare", str(remote))
            git("init", "--initial-branch", "main", str(seed))
            (seed / "Example" / "deploy").mkdir(parents=True)
            (seed / "Example" / "deploy" / "old.yaml").write_text("old\n", encoding="utf-8")
            (seed / "Example" / "kustomization.yaml").write_text(
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nimages:\n- name: example\n  newTag: old\n",
                encoding="utf-8",
            )
            git("add", ".", cwd=seed)
            git("-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial", cwd=seed)
            git("remote", "add", "origin", str(remote), cwd=seed)
            git("push", "origin", "main", cwd=seed)

            digest = "sha256:" + "c" * 64
            revision, _ = promote_snapshot(
                source,
                str(remote),
                "Example",
                "kustomization.yaml",
                "main",
                "a" * 40,
                {"example": {"newName": "registry/example", "digest": digest}},
            )

            rendered = git("--git-dir", str(remote), "show", f"{revision}:Example/kustomization.yaml").stdout
            self.assertIn(f"digest: {digest}", rendered)
            self.assertNotIn("newTag: old", rendered)

            rollback_revision = rollback_snapshot(str(remote), "main", revision)
            rolled_back = git("--git-dir", str(remote), "show", f"{rollback_revision}:Example/kustomization.yaml").stdout
            self.assertIn("newTag: old", rolled_back)
            self.assertNotIn(f"digest: {digest}", rolled_back)

    @patch("ci_templates.build._docker")
    def test_build_preserves_existing_dev_before_replacement(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
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
        builder = docker.call_args_list[3].args[0][-1]
        self.assertEqual(
            docker.call_args_list[3].args[0][:4],
            ["buildx", "create", "--driver", "docker-container"],
        )
        self.assertEqual(docker.call_args_list[4].args[0][:4], ["buildx", "build", "--builder", builder])
        self.assertEqual(docker.call_args_list[5], call(["buildx", "rm", "--force", builder], cwd=".", check=False))

    @patch("ci_templates.build._docker")
    def test_first_build_does_not_require_previous_image(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ]

        build_service(config().services[0])

        self.assertEqual(docker.call_count, 4)
        builder = docker.call_args_list[1].args[0][-1]
        self.assertEqual(docker.call_args_list[2].args[0][:4], ["buildx", "build", "--builder", builder])
        self.assertIn("--push", docker.call_args_list[2].args[0])

    @patch("ci_templates.build._docker")
    def test_failed_build_removes_temporary_builder(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
            subprocess.CalledProcessError(1, ["docker", "buildx", "build"]),
            subprocess.CompletedProcess([], 0),
        ]

        with self.assertRaises(BuildError):
            build_service(config().services[0])

        builder = docker.call_args_list[1].args[0][-1]
        self.assertEqual(docker.call_args_list[-1], call(["buildx", "rm", "--force", builder], cwd=".", check=False))

    @patch("ci_templates.build._docker")
    def test_build_configures_private_registry_ca(self, docker):
        import tempfile
        observed = []

        def run(args, **_kwargs):
            if args[:2] == ["buildx", "create"]:
                config_path = Path(args[args.index("--buildkitd-config") + 1])
                observed.append(config_path.read_text(encoding="utf-8"))
            return subprocess.CompletedProcess([], 1 if args[0] == "pull" else 0)

        docker.side_effect = run
        with tempfile.TemporaryDirectory() as directory:
            ca_path = Path(directory) / "registry-ca.crt"
            ca_path.write_text("test CA\n", encoding="utf-8")
            with patch.dict("os.environ", {"CI_REGISTRY_CA_FILE": str(ca_path)}):
                build_service(config().services[0])

        self.assertEqual(observed, [f'[registry."org"]\n  ca = ["{ca_path}"]\n'])

    @patch("ci_templates.build.subprocess.run")
    def test_image_digest_accepts_legacy_buildx_output(self, run):
        digest = "sha256:" + "a" * 64
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"Name: org/gateway:dev\nMediaType: application/vnd.oci.image.manifest.v1+json\nDigest: {digest}\n",
        )

        self.assertEqual(image_digest("org/gateway:dev"), digest)

    @patch("ci_templates.build.subprocess.run")
    def test_image_digest_accepts_formatted_buildx_output(self, run):
        digest = "sha256:" + "b" * 64
        run.return_value = subprocess.CompletedProcess([], 0, stdout=f"{digest}\n")

        self.assertEqual(image_digest("org/gateway:dev"), digest)

    @patch("ci_templates.build.subprocess.run")
    def test_image_digest_rejects_unrecognized_output(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="Digest: unavailable\n")

        with self.assertRaisesRegex(BuildError, "cannot resolve digest"):
            image_digest("org/gateway:dev")

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
