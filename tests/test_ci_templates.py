import json
from pathlib import Path
import hashlib
import io
import tarfile
import subprocess
import sys
from unittest.mock import call, patch

import unittest

from ci_templates.changes import affected_services, build_release_context, classify_release_paths, read_release_context
from ci_templates.config import ConfigError, Pipeline
from ci_templates.gitops import _git, promote_snapshot, rollback_snapshot, update_images
from ci_templates.release import render_aggregate_release
from ci_templates.versions import aggregate_release_tag, service_tag
from ci_templates.charts import Chart, ChartError, _extract_chart, _relative_path, _validate_rendered, load_chart_manifest
from ci_templates.build import BuildError, _docker, build_jobs, build_service, image_digest
from ci_templates.argocd import ArgoError, _has_revision, _observed_revisions, _ready_state, wait_application
from ci_templates.github import create_and_push_tag, fast_forward_main


def config() -> Pipeline:
    return Pipeline.from_mapping({
        "project": "example",
        "source_repo": "org/example",
        "gitops_repo": "org/gitops",
        "gitops_path": "Example",
        "gitops_branch": "main",
        "shared_paths": ["pkg", "idl"],
        "aggregate_release_prefix": "example",
        "aggregate_version_file": "VERSION",
        "base_images": [{"source": "example/base:source", "destination": "example/base:cached"}],
        "services": [{
            "name": "gateway", "source_path": "services/gateway", "version_file": "services/gateway/VERSION",
            "dockerfile": "services/gateway/Dockerfile", "context": ".", "image_repository": "org/gateway",
            "deploy_snapshot": "deploy/gateway",
        }],
    })


def multi_service_config() -> Pipeline:
    return Pipeline.from_mapping({
        "project": "Knowledge-Core",
        "source_repo": "org/example",
        "gitops_repo": "org/gitops",
        "gitops_path": "Example",
        "gitops_branch": "main",
        "shared_paths": ["pkg", "Makefile"],
        "aggregate_release_prefix": "knowledge-core",
        "aggregate_version_file": "VERSION",
        "base_images": [{"source": "example/base:source", "destination": "example/base:cached"}],
        "services": [
            {
                "name": "gateway", "source_path": "services/gateway", "version_file": "services/gateway/VERSION",
                "dockerfile": "docker/gateway/Dockerfile", "context": ".", "image_repository": "org/gateway",
                "deploy_snapshot": "deploy/gateway",
            },
            {
                "name": "identity", "source_path": "services/identity", "version_file": "services/identity/VERSION",
                "dockerfile": "docker/identity/Dockerfile", "context": ".", "image_repository": "org/identity",
                "deploy_snapshot": "deploy/identity",
            },
            {
                "name": "knowledge", "source_path": "services/knowledge", "version_file": "services/knowledge/VERSION",
                "dockerfile": "docker/knowledge/Dockerfile", "context": ".", "image_repository": "org/knowledge",
                "deploy_snapshot": "deploy/knowledge",
            },
        ],
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

    def test_dockerfile_change_rebuilds_service(self):
        self.assertEqual(affected_services(config(), ["services/gateway/Dockerfile"]), ("gateway",))


    def test_unrelated_changes_are_ignored(self):
        self.assertEqual(affected_services(config(), ["docs/README.md"]), ())


    def test_service_tag(self):
        self.assertEqual(service_tag("gateway", (1, 2, 3)), "gateway-v1.2.3")

    def test_aggregate_release_tag_increments_only_aggregate_tags(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            (root / "VERSION").write_text("0.1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "VERSION"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "tag", "knowledge-core-v0.1.1"], check=True)
            subprocess.run(["git", "-C", str(root), "tag", "v0.1.0"], check=True)
            self.assertEqual(aggregate_release_tag("knowledge-core", (0, 1, 0), cwd=str(root)), "v0.1.0")
            (root / "VERSION").write_text("0.1\nchanged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "VERSION"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "change"], check=True)
            self.assertEqual(aggregate_release_tag("knowledge-core", (0, 1, 0), cwd=str(root)), "v0.1.2")
            (root / "VERSION").write_text("0.1\nagain\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "VERSION"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "again"], check=True)
            subprocess.run(["git", "-C", str(root), "tag", "v0.1.2"], check=True)
            self.assertEqual(aggregate_release_tag("knowledge-core", (0, 1, 0), cwd=str(root)), "v0.1.2")

    def test_aggregate_release_tag_reuses_legacy_prefix_tag_on_head(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            (root / "VERSION").write_text("0.1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "VERSION"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            subprocess.run(["git", "-C", str(root), "tag", "knowledge-core-v0.1.7"], check=True)
            self.assertEqual(aggregate_release_tag("knowledge-core", (0, 1, 0), cwd=str(root)), "knowledge-core-v0.1.7")

    def test_release_context_redacts_sensitive_values(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
            (root / "services/gateway").mkdir(parents=True)
            (root / "services/gateway/config.go").write_text(
                'const token = "first"\nconst databaseURL = "postgres://user:first@db/knowledge"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            (root / "services/gateway/config.go").write_text(
                'const token = "second"\nconst databaseURL = "postgres://user:second@db/knowledge"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "change"], check=True)
            config_value = config()
            context = build_release_context(config_value, "HEAD^", "HEAD", ["services/gateway/config.go"], cwd=str(root))
            diff = context["services"]["gateway"]["diff"]
            self.assertGreaterEqual(diff.count("[REDACTED SENSITIVE VALUE]"), 2)
            self.assertNotIn("second@db", diff)
            self.assertEqual(context["shared"]["paths"], [])

            context_path = root / "release.json"
            context_path.write_text(json.dumps(context), encoding="utf-8")
            self.assertEqual(read_release_context(context_path)["base"], "HEAD^")

    def test_classify_makefile_and_dockerfiles_as_shared_only(self):
        shared, exclusive = classify_release_paths(
            multi_service_config(),
            ["Makefile", "docker/gateway/Dockerfile", "docker/identity/Dockerfile", "docker/knowledge/Dockerfile"],
        )
        self.assertEqual(
            shared,
            ["Makefile", "docker/gateway/Dockerfile", "docker/identity/Dockerfile", "docker/knowledge/Dockerfile"],
        )
        self.assertEqual(exclusive, {})

    def test_classify_keeps_service_business_paths_exclusive(self):
        shared, exclusive = classify_release_paths(
            multi_service_config(),
            ["Makefile", "services/knowledge/internal/worker/worker.go"],
        )
        self.assertEqual(shared, ["Makefile"])
        self.assertEqual(exclusive, {"knowledge": ["services/knowledge/internal/worker/worker.go"]})

    def test_render_aggregate_release_omits_empty_sections(self):
        body = render_aggregate_release(
            "v0.1.6",
            "- Cap BUILD_JOBS at three quarters of host CPUs.",
            [],
            ["gateway", "identity", "knowledge"],
        )
        self.assertEqual(
            body,
            "# v0.1.6\n"
            "\n"
            "## Shared changes\n"
            "\n"
            "- Cap BUILD_JOBS at three quarters of host CPUs.\n"
            "\n"
            "## Deployed services\n"
            "\n"
            "- gateway\n"
            "- identity\n"
            "- knowledge\n",
        )
        body = render_aggregate_release(
            "v0.1.7",
            "",
            [("knowledge", "- Wake workers with PostgreSQL NOTIFY.")],
            ["knowledge"],
        )
        self.assertIn("## Service-specific changes\n\n### knowledge\n", body)
        self.assertNotIn("## Shared changes", body)
        self.assertNotIn("Service versions", body)

    def test_argo_revision_supports_single_and_multi_source_applications(self):
        revision = "a" * 40
        self.assertTrue(_has_revision({"revision": revision}, revision))
        self.assertTrue(_has_revision({"revisions": [revision, revision]}, revision))
        self.assertFalse(_has_revision({"revisions": ["b" * 40]}, revision))

    def test_argo_ready_accepts_history_revision_when_sync_sha_lags(self):
        desired = "a" * 40
        previous = "b" * 40
        payload = {
            "status": {
                "sync": {"revision": previous, "status": "Synced"},
                "health": {"status": "Healthy"},
                "history": [{"revision": desired}],
            }
        }
        self.assertIn(desired, _observed_revisions(payload))
        ready, _ = _ready_state(payload, desired)
        self.assertTrue(ready)

    @patch.dict("ci_templates.argocd.os.environ", {"KUBECONFIG": "/secrets/kubeconfig"}, clear=False)
    @patch("ci_templates.argocd.time.sleep", return_value=None)
    @patch("ci_templates.argocd.subprocess.run")
    def test_argo_wait_refreshes_and_accepts_updated_revision(self, run, _sleep):
        desired = "a" * 40
        stale = json.dumps({
            "status": {
                "sync": {"revision": "b" * 40, "status": "Synced"},
                "health": {"status": "Healthy"},
            }
        })
        current = json.dumps({
            "status": {
                "sync": {"revision": desired, "status": "Synced"},
                "health": {"status": "Healthy"},
            }
        })
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, stale, ""),
            subprocess.CompletedProcess([], 0, current, ""),
        ]

        payload = wait_application("argocd.example.invalid", "knowledge-core-gateway-dev", desired, timeout=30)

        self.assertEqual(payload["status"]["sync"]["revision"], desired)
        commands = [list(item.args[0]) for item in run.call_args_list]
        self.assertTrue(any("annotate" in command for command in commands), commands)
        self.assertTrue(
            any("argocd.argoproj.io/refresh=hard" in command for command in commands),
            commands,
        )

    @patch.dict("ci_templates.argocd.os.environ", {"KUBECONFIG": "/secrets/kubeconfig"}, clear=False)
    @patch("ci_templates.argocd.time.sleep", return_value=None)
    @patch("ci_templates.argocd.subprocess.run")
    def test_argo_wait_times_out_when_healthy_at_another_revision(self, run, _sleep):
        stale = json.dumps({
            "status": {
                "sync": {"revision": "b" * 40, "status": "Synced"},
                "health": {"status": "Healthy"},
            }
        })
        run.return_value = subprocess.CompletedProcess([], 0, stale, "")
        ticks = {"count": 0}

        def monotonic() -> float:
            ticks["count"] += 1
            return 0.0 if ticks["count"] < 8 else 601.0

        with patch("ci_templates.argocd.time.monotonic", side_effect=monotonic):
            with self.assertRaises(ArgoError) as raised:
                wait_application("argocd.example.internal", "knowledge-core-gateway-dev", "a" * 40, timeout=600)

        self.assertIn("revision=" + "b" * 40, str(raised.exception))
        self.assertIn("sync=Synced", str(raised.exception))
        self.assertIn("health=Healthy", str(raised.exception))

    @patch.dict("ci_templates.github.os.environ", {"GITHUB_TOKEN": "test-token"}, clear=False)
    @patch("ci_templates.github.subprocess.run")
    def test_main_promotion_configures_tag_identity_and_credentials(self, run):
        fast_forward_main(cwd="/workspace")

        self.assertEqual([item.args[0] for item in run.call_args_list], [
            ["git", "config", "user.name", "happyladysauce-ci"],
            ["git", "config", "user.email", "happyladysauce-ci@noreply.local"],
            ["git", "fetch", "origin", "main", "dev"],
            ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
            ["git", "push", "origin", "HEAD:main"],
        ])
        env = run.call_args_list[0].kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertIn("$GITHUB_TOKEN", env["GIT_CONFIG_VALUE_0"])
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    @patch.dict("ci_templates.github.os.environ", {"GITHUB_TOKEN": "test-token"}, clear=False)
    @patch("ci_templates.github.subprocess.run")
    def test_tag_push_uses_the_same_credentials(self, run):
        create_and_push_tag("gateway-v0.1.1", "summary", cwd="/workspace")

        self.assertEqual([item.args[0] for item in run.call_args_list], [
            ["git", "config", "user.name", "happyladysauce-ci"],
            ["git", "config", "user.email", "happyladysauce-ci@noreply.local"],
            ["git", "rev-parse", "--verify", "--quiet", "refs/tags/gateway-v0.1.1^{}"],
            ["git", "tag", "-a", "gateway-v0.1.1", "-m", "summary", "HEAD"],
            ["git", "push", "origin", "gateway-v0.1.1"],
        ])
        self.assertEqual(run.call_args_list[-1].kwargs["env"]["GIT_CONFIG_KEY_0"], "credential.helper")

    @patch("ci_templates.__main__.create_release")
    @patch("ci_templates.__main__.create_and_push_tag")
    @patch("ci_templates.__main__.fast_forward_main")
    @patch("ci_templates.__main__.summarize_with_deepseek", return_value="summary")
    @patch("ci_templates.__main__.subprocess.run")
    @patch("ci_templates.__main__.load_config")
    def test_release_creates_one_aggregate_github_release(
        self, load_config_mock, run, summarize, fast_forward, push_tag, create_release_mock
    ):
        load_config_mock.return_value = config()
        run.return_value = subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n")

        from ci_templates.__main__ import main

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            changes_file = Path(directory) / "changes.json"
            changes_file.write_text(
                json.dumps({
                    "shared": {"paths": [], "diff": ""},
                    "services": {"gateway": {"paths": ["services/gateway/a.go"], "diff": "+ feature"}},
                }),
                encoding="utf-8",
            )
            with (
                patch.dict("ci_templates.__main__.os.environ", {}, clear=False),
                patch("ci_templates.__main__.read_version", return_value=(1, 2, 3)),
                patch("ci_templates.__main__.aggregate_release_tag", return_value="v1.2.1"),
            ):
                self.assertEqual(main(["release", "--services", "gateway", "--changes-file", str(changes_file)]), 0)

        push_tag.assert_called_once_with("v1.2.1", "v1.2.1", cwd=".")
        create_release_mock.assert_called_once_with(
            "org/example",
            "v1.2.1",
            "a" * 40,
            "# v1.2.1\n\n## Service-specific changes\n\n### gateway\n\nsummary\n\n## Deployed services\n\n- gateway\n",
            name="v1.2.1",
        )
        summarize.assert_called_once()
        self.assertFalse(summarize.call_args.kwargs.get("shared"))

    @patch("ci_templates.__main__.create_release")
    @patch("ci_templates.__main__.create_and_push_tag")
    @patch("ci_templates.__main__.fast_forward_main")
    @patch("ci_templates.__main__.summarize_with_deepseek", return_value="- shared summary")
    @patch("ci_templates.__main__.subprocess.run")
    @patch("ci_templates.__main__.load_config")
    def test_release_shared_only_skips_service_sections(
        self, load_config_mock, run, summarize, fast_forward, push_tag, create_release_mock
    ):
        load_config_mock.return_value = multi_service_config()
        run.return_value = subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n")

        from ci_templates.__main__ import main

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            changes_file = Path(directory) / "changes.json"
            changes_file.write_text(
                json.dumps({
                    "shared": {"paths": ["Makefile", "docker/gateway/Dockerfile"], "diff": "+ BUILD_JOBS"},
                    "services": {},
                }),
                encoding="utf-8",
            )
            with (
                patch.dict("ci_templates.__main__.os.environ", {}, clear=False),
                patch("ci_templates.__main__.read_version", return_value=(0, 1, 5)),
                patch("ci_templates.__main__.aggregate_release_tag", return_value="v0.1.6"),
            ):
                self.assertEqual(
                    main(["release", "--services", "gateway,identity,knowledge", "--changes-file", str(changes_file)]),
                    0,
                )

        push_tag.assert_called_once_with(
            "v0.1.6",
            "v0.1.6",
            cwd=".",
        )
        body = create_release_mock.call_args.args[3]
        self.assertIn("## Shared changes", body)
        self.assertNotIn("## Service-specific changes", body)
        self.assertIn("- gateway\n- identity\n- knowledge\n", body)
        summarize.assert_called_once()
        self.assertTrue(summarize.call_args.kwargs.get("shared"))


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
            subprocess.CompletedProcess([], 0),  # pull
            subprocess.CompletedProcess([], 0),  # tag
            subprocess.CompletedProcess([], 0),  # push previous
            subprocess.CompletedProcess([], 0),  # buildx inspect (exists)
            subprocess.CompletedProcess([], 0),  # buildx build
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
        self.assertEqual(
            docker.call_args_list[3],
            call(["buildx", "inspect", "ci-templates"], cwd=".", check=False),
        )
        build_args = docker.call_args_list[4].args[0]
        self.assertEqual(build_args[:4], ["buildx", "build", "--builder", "ci-templates"])
        self.assertIn("--build-arg", build_args)
        self.assertIn(f"BUILD_JOBS={build_jobs()}", build_args)
        self.assertFalse(any(args.args[0][:2] == ["buildx", "rm"] for args in docker.call_args_list))

    @patch("ci_templates.build._docker")
    def test_first_build_does_not_require_previous_image(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 1),  # pull miss
            subprocess.CompletedProcess([], 0),  # inspect exists
            subprocess.CompletedProcess([], 0),  # build
        ]

        build_service(config().services[0])

        self.assertEqual(docker.call_count, 3)
        build_args = docker.call_args_list[2].args[0]
        self.assertEqual(build_args[:4], ["buildx", "build", "--builder", "ci-templates"])
        self.assertIn("--push", build_args)

    @patch("ci_templates.build._docker")
    def test_failed_build_keeps_stable_builder(self, docker):
        docker.side_effect = [
            subprocess.CompletedProcess([], 1),  # pull miss
            subprocess.CompletedProcess([], 0),  # inspect exists
            subprocess.CalledProcessError(1, ["docker", "buildx", "build"]),
        ]

        with self.assertRaises(BuildError):
            build_service(config().services[0])

        self.assertFalse(any(args.args[0][:2] == ["buildx", "rm"] for args in docker.call_args_list))

    @patch("ci_templates.build._docker")
    def test_build_configures_private_registry_ca(self, docker):
        import tempfile
        observed = []

        def run(args, **_kwargs):
            if args[:2] == ["buildx", "inspect"]:
                return subprocess.CompletedProcess([], 1)
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

        self.assertEqual(len(observed), 1)
        self.assertIn("[worker.oci]", observed[0])
        self.assertIn(f"max-parallelism = {build_jobs()}", observed[0])
        self.assertIn(f'[registry."org"]\n  ca = ["{ca_path}"]\n', observed[0])

    def test_build_jobs_uses_three_quarters_of_cpus(self):
        with patch("ci_templates.build.os.cpu_count", return_value=8):
            self.assertEqual(build_jobs(), 6)
        with patch("ci_templates.build.os.cpu_count", return_value=1):
            self.assertEqual(build_jobs(), 1)
        with patch("ci_templates.build.os.cpu_count", return_value=None):
            self.assertEqual(build_jobs(), 1)

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
