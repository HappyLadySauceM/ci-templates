from __future__ import annotations

import argparse
import json
import sys
import os
import subprocess
from pathlib import Path

from .changes import affected_services, build_release_context, changed_paths, deploy_changed, deploy_services, read_release_context, release_services, resolve_revision, write_release_context
from .config import ConfigError, load_config
from .gitops import sync_snapshot, promote_snapshot, rollback_snapshot
from .build import build_service, discard_previous, delete_previous, restore_previous, prewarm_base_images, image_digest
from .argocd import wait_applications, wait_targets
from .smoke import run as run_smoke, run_kubernetes
from .github import create_and_push_tag, create_release, fast_forward_main, set_commit_status
from .release import render_aggregate_release, summarize_release_with_deepseek, summarize_with_deepseek
from .versions import aggregate_release_tag, next_patch, read_version, service_tag
from .charts import ChartError, check_charts, format_result, mirror_charts
from .harbor import HarborClient, ImageRef


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-templates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", default=None)

    changes = subparsers.add_parser("changes")
    changes.add_argument("--config", default=None)
    changes.add_argument("--base", required=True)
    changes.add_argument("--head", default="HEAD")
    changes.add_argument("--details-file", default=None)

    versions = subparsers.add_parser("versions")
    versions.add_argument("--config", default=None)
    versions.add_argument("--service", default=None)
    versions.add_argument("--repo", default=".")

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--target", required=True)

    promote = subparsers.add_parser("promote-snapshot")
    promote.add_argument("--config", default=None)
    promote.add_argument("--source-sha", required=True)
    promote.add_argument("--deploy-source", default="deploy")

    rollback = subparsers.add_parser("rollback-snapshot")
    rollback.add_argument("--config", default=None)
    rollback.add_argument("--revision", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--config", default=None)
    build.add_argument("--service", required=True)
    build.add_argument("--repo", default=".")
    build.add_argument("--tag", default="dev")
    build.add_argument("--artifact-manifest", default=None)
    build.add_argument("--preserve-previous", action="store_true")
    build.add_argument("--reuse-existing", action="store_true")

    cleanup = subparsers.add_parser("cleanup-previous")
    cleanup.add_argument("--config", default=None)
    cleanup.add_argument("--service", required=True)
    cleanup.add_argument("--repo", default=".")

    restore = subparsers.add_parser("restore-previous")
    restore.add_argument("--config", default=None)
    restore.add_argument("--service", required=True)
    restore.add_argument("--repo", default=".")

    candidate_cleanup = subparsers.add_parser("cleanup-candidate")
    candidate_cleanup.add_argument("--config", default=None)
    candidate_cleanup.add_argument("--service", required=True)
    candidate_cleanup.add_argument("--tag", required=True)

    candidate_promote = subparsers.add_parser("promote-candidate")
    candidate_promote.add_argument("--config", default=None)
    candidate_promote.add_argument("--service", required=True)
    candidate_promote.add_argument("--tag", required=True)
    candidate_promote.add_argument("--repo", default=".")

    argo = subparsers.add_parser("argo-wait")
    argo.add_argument("--config", default=None)
    argo.add_argument("--revision", required=True)
    argo.add_argument("--services", default="")

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--config", default=None)
    smoke.add_argument("--repo", default=".")

    release = subparsers.add_parser("release")
    release.add_argument("--config", default=None)
    release.add_argument("--services", required=True)
    release.add_argument("--repo", default=".")
    release.add_argument("--changes-file", default=None)
    release.add_argument("--summary-file", default=None)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("--config", default=None)
    summarize.add_argument("--services", required=True)
    summarize.add_argument("--changes-file", required=True)
    summarize.add_argument("--output", required=True)
    summarize.add_argument("--repo", default=".")

    prewarm = subparsers.add_parser("prewarm")
    prewarm.add_argument("--config", default=None)
    prewarm.add_argument("--repo", default=".")

    status = subparsers.add_parser("status")
    status.add_argument("--config", default=None)
    status.add_argument("--sha", required=True)
    status.add_argument("--state", required=True, choices=("pending", "success", "failure", "error"))
    status.add_argument("--description", required=True)
    status.add_argument("--context", default=None)
    status.add_argument("--target-url", default="")

    charts_check = subparsers.add_parser("charts-check")
    charts_check.add_argument("--manifest", required=True)
    charts_check.add_argument("--root", default=".")
    charts_check.add_argument("--allow-missing-vendors", action="store_true")

    charts_mirror = subparsers.add_parser("charts-mirror")
    charts_mirror.add_argument("--manifest", required=True)
    charts_mirror.add_argument("--root", default=".")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            print(json.dumps({"project": config.project, "services": [service.name for service in config.services]}, sort_keys=True))
        elif args.command == "changes":
            config = load_config(args.config)
            paths = changed_paths(args.base, args.head)
            if args.details_file:
                resolved_base = resolve_revision(args.base) if args.base and set(args.base) != {"0"} else args.base
                resolved_head = resolve_revision(args.head)
                write_release_context(
                    args.details_file,
                    build_release_context(config, resolved_base, resolved_head, paths),
                )
            build = list(affected_services(config, paths))
            deploy = list(deploy_services(config, paths))
            release = list(release_services(config, paths))
            print(json.dumps({
                "paths": paths,
                "services": release,
                "build_services": build,
                "deploy_services": deploy,
                "release_services": release,
                "deploy_changed": deploy_changed(config, paths),
            }, sort_keys=True))
        elif args.command == "versions":
            config = load_config(args.config)
            selected = [service for service in config.services if args.service is None or service.name == args.service]
            output = {}
            for service in selected:
                version = read_version(Path(args.repo) / service.version_file)
                release = next_patch(service.name, version, cwd=args.repo)
                output[service.name] = {"version": ".".join(map(str, version)), "tag": service_tag(service.name, release)}
            print(json.dumps(output, sort_keys=True))
        elif args.command == "snapshot":
            sync_snapshot(args.source, args.target)
            print(json.dumps({"source": args.source, "target": args.target}, sort_keys=True))
        elif args.command == "promote-snapshot":
            config = load_config(args.config)
            overrides = json.loads(os.environ.get("CI_GITOPS_IMAGE_OVERRIDES_JSON", "{}"))
            revision, base_revision = promote_snapshot(
                args.deploy_source,
                config.gitops_repo,
                config.gitops_path,
                config.gitops_kustomization,
                config.gitops_branch,
                args.source_sha,
                overrides,
                identity_name=config.git_identity_name,
                identity_email=config.git_identity_email,
            )
            print(json.dumps({"gitops_revision": revision, "gitops_base_revision": base_revision}, sort_keys=True))
        elif args.command == "rollback-snapshot":
            config = load_config(args.config)
            print(json.dumps({"gitops_revision": rollback_snapshot(config.gitops_repo, config.gitops_branch, args.revision, identity_name=config.git_identity_name, identity_email=config.git_identity_email)}, sort_keys=True))
        elif args.command in {"build", "cleanup-previous"}:
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            if args.command == "build":
                image = build_service(
                    service,
                    tag=args.tag,
                    cwd=args.repo,
                    preserve_previous=args.preserve_previous,
                    artifact_manifest=args.artifact_manifest,
                    reuse_existing=args.reuse_existing,
                    active_tag=config.active_image_tag,
                    previous_tag=config.previous_image_tag,
                    cache_tag=config.cache_image_tag,
                    buildkit_reserved_space=config.buildkit_reserved_space,
                    buildkit_max_used_space=config.buildkit_max_used_space,
                    buildkit_min_free_space=config.buildkit_min_free_space,
                )
                print(json.dumps({"service": service.name, "kustomize_name": service.kustomize_name, "image": image, "digest": image_digest(image, cwd=args.repo)}, sort_keys=True))
            else:
                discard_previous(service, cwd=args.repo, active_tag=config.active_image_tag, previous_tag=config.previous_image_tag)
                delete_previous(service, config.harbor_registry, previous_tag=config.previous_image_tag)
        elif args.command == "restore-previous":
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            restore_previous(service, cwd=args.repo, active_tag=config.active_image_tag, previous_tag=config.previous_image_tag)
        elif args.command == "cleanup-candidate":
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            HarborClient(config.harbor_registry).delete_tag(ImageRef.parse(f"{service.image_repository}:{args.tag}"))
        elif args.command == "promote-candidate":
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            source = ImageRef.parse(f"{service.image_repository}:{args.tag}")
            destination = ImageRef.parse(f"{service.image_repository}:{config.active_image_tag}")
            digest = HarborClient(config.harbor_registry).promote_tag(source, destination)
            print(json.dumps({"service": service.name, "tag": args.tag, "active_tag": destination.tag, "digest": digest}, sort_keys=True))
        elif args.command == "argo-wait":
            config = load_config(args.config)
            if not config.argocd_server:
                raise ConfigError("argocd_server is required")
            selected = [item.strip() for item in args.services.split(",") if item.strip()]
            overrides = json.loads(os.environ.get("CI_GITOPS_IMAGE_OVERRIDES_JSON", "{}") or "{}")
            if not isinstance(overrides, dict):
                raise ConfigError("CI_GITOPS_IMAGE_OVERRIDES_JSON must be an object")
            if selected:
                services = [service for service in config.services if service.name in selected]
                missing = [name for name in selected if name not in {service.name for service in services}]
                if missing:
                    raise ConfigError(f"unknown service: {', '.join(missing)}")
                targets = wait_targets(services, overrides, config.application_suffix)
            else:
                if not config.argocd_application:
                    raise ConfigError("argocd_server and argocd_application are required")
                targets = {config.argocd_application: ()}
            wait_applications(
                config.argocd_server,
                tuple(targets),
                args.revision,
                expected_images=targets,
                argocd_namespace=config.argocd_namespace,
            )
        elif args.command == "smoke":
            config = load_config(args.config)
            if os.environ.get("KUBECONFIG") and config.smoke_endpoints:
                namespace = os.environ.get("APPLICATION_NAMESPACE") or config.smoke_namespace
                run_kubernetes(
                    namespace=namespace,
                    kubeconfig=os.environ["KUBECONFIG"],
                    endpoints=config.smoke_endpoints,
                )
            else:
                run_smoke(config.smoke_command, cwd=args.repo, env=dict(config.smoke_env))
        elif args.command == "summarize":
            config = load_config(args.config)
            selected = {item.strip() for item in args.services.split(",") if item.strip()}
            deployed = [service.name for service in config.services if service.name in selected]
            context = read_release_context(args.changes_file)
            if not deployed and not any(
                (bucket.get("paths") or bucket.get("diff"))
                for bucket in [context.get("shared", {})]
                if isinstance(bucket, dict)
            ):
                raise ConfigError("no affected services or shared changes selected for summary")
            aggregate_version = read_version(Path(args.repo) / config.aggregate_version_file)
            aggregate_tag = aggregate_release_tag(config.aggregate_release_prefix, aggregate_version, cwd=args.repo)
            body = summarize_release_with_deepseek(config.deepseek_model, aggregate_tag, context, deployed, config.release_language)
            destination = Path(args.output)
            destination.write_text(body, encoding="utf-8")
            destination.chmod(0o600)
            print(json.dumps({"summary_file": str(destination), "release": aggregate_tag}, sort_keys=True))
        elif args.command == "release":
            config = load_config(args.config)
            selected = {item.strip() for item in args.services.split(",") if item.strip()}
            deployed = [service.name for service in config.services if service.name in selected]
            changes_file = args.changes_file or os.environ.get("CI_RELEASE_CHANGES_FILE", "")
            if not changes_file:
                raise ConfigError("CI_RELEASE_CHANGES_FILE or --changes-file is required before main promotion")
            context = read_release_context(changes_file)
            current_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=args.repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            context_head = context.get("head")
            if context_head and context_head != current_commit:
                raise ConfigError("release change context does not match the checked-out commit")
            if not deployed and not any(
                (bucket.get("paths") or bucket.get("diff"))
                for bucket in [context.get("shared", {})]
                if isinstance(bucket, dict)
            ):
                raise ConfigError("no affected services or shared changes selected for release")
            aggregate_version = read_version(Path(args.repo) / config.aggregate_version_file)
            aggregate_tag = aggregate_release_tag(config.aggregate_release_prefix, aggregate_version, cwd=args.repo)
            if args.summary_file:
                try:
                    release_body = Path(args.summary_file).read_text(encoding="utf-8")
                except OSError as exc:
                    raise ConfigError(f"cannot read release summary: {exc}") from exc
                if not release_body.strip():
                    raise ConfigError("release summary is empty")
            else:
                shared_changes = context.get("shared")
                shared_summary = ""
                if isinstance(shared_changes, dict) and (shared_changes.get("paths") or shared_changes.get("diff")):
                    shared_summary = summarize_with_deepseek(
                        config.deepseek_model,
                        config.aggregate_release_prefix,
                        aggregate_tag,
                        shared_changes,
                        config.release_language,
                        shared=True,
                    )
                service_entries: list[tuple[str, str]] = []
                for service in config.services:
                    service_changes = context.get("services", {}).get(service.name)
                    if not isinstance(service_changes, dict):
                        continue
                    if not (service_changes.get("paths") or service_changes.get("diff")):
                        continue
                    summary = summarize_with_deepseek(
                        config.deepseek_model,
                        service.name,
                        aggregate_tag,
                        service_changes,
                        config.release_language,
                    )
                    service_entries.append((service.name, summary))
                if not shared_summary and not service_entries:
                    raise ConfigError("release change context has no shared or service-specific changes")
                release_body = render_aggregate_release(aggregate_tag, shared_summary, service_entries, deployed)
            git_identity = {}
            if config.git_identity_name != "ci-bot" or config.git_identity_email != "ci-bot@noreply.local":
                git_identity = {
                    "identity_name": config.git_identity_name,
                    "identity_email": config.git_identity_email,
                }
            branch_options = {}
            if config.release_branch != "main" or config.development_branch != "dev":
                branch_options = {
                    "branch": config.release_branch,
                    "development_branch": config.development_branch,
                }
            fast_forward_main(cwd=args.repo, **branch_options, **git_identity)
            target_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=args.repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            create_and_push_tag(aggregate_tag, aggregate_tag, cwd=args.repo, **git_identity)
            create_release(
                config.source_repo,
                aggregate_tag,
                target_commit,
                release_body,
                name=aggregate_tag,
            )
            print(json.dumps({"deployed": deployed, "release": aggregate_tag}, sort_keys=True))
        elif args.command == "prewarm":
            config = load_config(args.config)
            prewarm_base_images(config.base_images, cwd=args.repo)
        elif args.command == "status":
            config = load_config(args.config)
            set_commit_status(
                config.source_repo,
                args.sha,
                args.state,
                args.description,
                args.context or config.status_context,
                args.target_url,
            )
        elif args.command == "charts-check":
            print(format_result(check_charts(args.manifest, args.root, not args.allow_missing_vendors)))
        elif args.command == "charts-mirror":
            print(format_result(mirror_charts(args.manifest, args.root)))
    except (ChartError, ConfigError, OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
