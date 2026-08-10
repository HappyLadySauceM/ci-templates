from __future__ import annotations

import argparse
import json
import sys
import os

from .changes import affected_services, changed_paths
from .config import ConfigError, load_config
from .gitops import sync_snapshot, promote_snapshot, rollback_snapshot
from .build import build_service, discard_previous, delete_previous, restore_previous, prewarm_base_images, image_digest
from .argocd import wait_application
from .smoke import run as run_smoke, run_kubernetes
from .github import create_and_push_tag, create_release, fast_forward_main, set_commit_status
from .release import summarize_with_deepseek
from .versions import next_patch, read_version, service_tag
from .charts import ChartError, check_charts, format_result, mirror_charts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-templates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", default=None)

    changes = subparsers.add_parser("changes")
    changes.add_argument("--config", default=None)
    changes.add_argument("--base", required=True)
    changes.add_argument("--head", default="HEAD")

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

    cleanup = subparsers.add_parser("cleanup-previous")
    cleanup.add_argument("--config", default=None)
    cleanup.add_argument("--service", required=True)
    cleanup.add_argument("--repo", default=".")

    restore = subparsers.add_parser("restore-previous")
    restore.add_argument("--config", default=None)
    restore.add_argument("--service", required=True)
    restore.add_argument("--repo", default=".")

    argo = subparsers.add_parser("argo-wait")
    argo.add_argument("--config", default=None)
    argo.add_argument("--revision", required=True)

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--config", default=None)
    smoke.add_argument("--repo", default=".")

    release = subparsers.add_parser("release")
    release.add_argument("--config", default=None)
    release.add_argument("--services", required=True)
    release.add_argument("--repo", default=".")

    prewarm = subparsers.add_parser("prewarm")
    prewarm.add_argument("--config", default=None)
    prewarm.add_argument("--repo", default=".")

    status = subparsers.add_parser("status")
    status.add_argument("--config", default=None)
    status.add_argument("--sha", required=True)
    status.add_argument("--state", required=True, choices=("pending", "success", "failure", "error"))
    status.add_argument("--description", required=True)
    status.add_argument("--context", default="knowledge-core/smoke")
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
            print(json.dumps({"paths": paths, "services": list(affected_services(config, paths))}, sort_keys=True))
        elif args.command == "versions":
            config = load_config(args.config)
            selected = [service for service in config.services if args.service is None or service.name == args.service]
            output = {}
            for service in selected:
                version = read_version(service.version_file)
                release = next_patch(service.name, version, cwd=args.repo)
                output[service.name] = {"version": ".".join(map(str, version)), "tag": service_tag(service.name, release)}
            print(json.dumps(output, sort_keys=True))
        elif args.command == "snapshot":
            sync_snapshot(args.source, args.target)
            print(json.dumps({"source": args.source, "target": args.target}, sort_keys=True))
        elif args.command == "promote-snapshot":
            config = load_config(args.config)
            overrides = json.loads(os.environ.get("CI_GITOPS_IMAGE_OVERRIDES_JSON", "{}"))
            revision, base_revision = promote_snapshot(args.deploy_source, config.gitops_repo, config.gitops_path, config.gitops_kustomization, config.gitops_branch, args.source_sha, overrides)
            print(json.dumps({"gitops_revision": revision, "gitops_base_revision": base_revision}, sort_keys=True))
        elif args.command == "rollback-snapshot":
            config = load_config(args.config)
            print(json.dumps({"gitops_revision": rollback_snapshot(config.gitops_repo, config.gitops_branch, args.revision)}, sort_keys=True))
        elif args.command in {"build", "cleanup-previous"}:
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            if args.command == "build":
                image = build_service(service, cwd=args.repo)
                print(json.dumps({"service": service.name, "kustomize_name": service.kustomize_name, "image": image, "digest": image_digest(image, cwd=args.repo)}, sort_keys=True))
            else:
                discard_previous(service, cwd=args.repo)
                delete_previous(service, config.harbor_registry)
        elif args.command == "restore-previous":
            config = load_config(args.config)
            service = next((item for item in config.services if item.name == args.service), None)
            if service is None:
                raise ConfigError(f"unknown service: {args.service}")
            restore_previous(service, cwd=args.repo)
        elif args.command == "argo-wait":
            config = load_config(args.config)
            if not config.argocd_server or not config.argocd_application:
                raise ConfigError("argocd_server and argocd_application are required")
            wait_application(config.argocd_server, config.argocd_application, args.revision)
        elif args.command == "smoke":
            config = load_config(args.config)
            if os.environ.get("KUBECONFIG"):
                run_kubernetes(namespace=os.environ.get("APPLICATION_NAMESPACE", "knowledge-core-dev"), kubeconfig=os.environ["KUBECONFIG"])
            else:
                run_smoke(config.smoke_command, cwd=args.repo)
        elif args.command == "release":
            config = load_config(args.config)
            selected = {item.strip() for item in args.services.split(",") if item.strip()}
            metadata = json.loads(os.environ.get("CI_RELEASE_METADATA_JSON", "{}"))
            summaries: dict[str, str] = {}
            release_tags: list[tuple[str, str, str]] = []
            from .versions import read_version, next_patch, service_tag
            for service in config.services:
                if service.name not in selected:
                    continue
                base_version = read_version(service.version_file)
                release_version = next_patch(service.name, base_version, cwd=args.repo)
                tag = service_tag(service.name, release_version)
                summaries[service.name] = summarize_with_deepseek(config.deepseek_model, service.name, tag, {str(key): str(value) for key, value in metadata.items()})
                release_tags.append((service.name, tag, summaries[service.name]))
            if not release_tags:
                raise ConfigError("no affected services selected for release")
            fast_forward_main(cwd=args.repo)
            for service_name, tag, summary in release_tags:
                create_and_push_tag(tag, summary, cwd=args.repo)
                create_release(config.source_repo, tag, "HEAD", summary)
            print(json.dumps({"services": [item[0] for item in release_tags], "tags": [item[1] for item in release_tags]}, sort_keys=True))
        elif args.command == "prewarm":
            config = load_config(args.config)
            prewarm_base_images(config.base_images, cwd=args.repo)
        elif args.command == "status":
            config = load_config(args.config)
            set_commit_status(config.source_repo, args.sha, args.state, args.description, args.context, args.target_url)
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
