from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a pipeline configuration is incomplete or unsafe."""


def _safe_relative(value: Any, field: str, *, allow_dot: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty relative path")
    candidate = value.strip()
    path = Path(candidate)
    if (
        path.is_absolute()
        or candidate.startswith(("/", "\\"))
        or "\\" in candidate
        or ".." in path.parts
        or (not allow_dot and candidate in {"", "."})
    ):
        raise ConfigError(f"{field} must be a safe relative path")
    return candidate


@dataclass(frozen=True)
class Service:
    name: str
    source_path: str
    version_file: str
    dockerfile: str
    context: str
    image_repository: str
    deploy_snapshot: str
    kustomize_name: str
    artifact_group: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        project_slug: str = "project",
        *,
        legacy_defaults: bool = False,
    ) -> "Service":
        if not isinstance(value, dict):
            raise ConfigError("each service must be a mapping")
        fields = ("name", "source_path", "version_file", "dockerfile", "context", "image_repository", "deploy_snapshot")
        missing = [field for field in fields if not isinstance(value.get(field), str) or not value[field].strip()]
        if missing:
            raise ConfigError(f"service is missing fields: {', '.join(missing)}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value["name"]):
            raise ConfigError(f"invalid service name: {value['name']!r}")
        default_kustomize_name = f"knowledge-core-{value['name']}" if legacy_defaults else f"{project_slug}-{value['name']}"
        kustomize_name = value.get("kustomize_name", default_kustomize_name)
        if not isinstance(kustomize_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", kustomize_name):
            raise ConfigError("kustomize_name must be a non-empty string")
        artifact_group = value.get("artifact_group", "")
        if not isinstance(artifact_group, str):
            raise ConfigError("artifact_group must be a string")
        for field in ("source_path", "version_file", "dockerfile", "context", "deploy_snapshot"):
            _safe_relative(value[field], f"service.{field}")
        return cls(
            **{field: value[field] for field in fields},
            kustomize_name=kustomize_name,
            artifact_group=artifact_group,
        )


@dataclass(frozen=True)
class Pipeline:
    project: str
    source_repo: str
    gitops_repo: str
    gitops_path: str
    gitops_kustomization: str
    gitops_branch: str
    services: tuple[Service, ...]
    shared_paths: tuple[str, ...]
    harbor_registry: str
    harbor_project: str
    argocd_server: str
    argocd_application: str
    smoke_command: tuple[str, ...]
    release_model: str
    deepseek_model: str
    aggregate_release_prefix: str
    aggregate_version_file: str
    release_language: str
    base_images: tuple[tuple[str, str], ...]
    deploy_root: str
    schema_version: int = 1
    argocd_namespace: str = "argocd"
    application_suffix: str = "-dev"
    smoke_namespace: str = ""
    smoke_endpoints: tuple[tuple[str, str, str], ...] = ()
    smoke_env: tuple[tuple[str, str], ...] = ()
    active_image_tag: str = "dev"
    previous_image_tag: str = "previous"
    cache_image_tag: str = "buildcache"
    candidate_tag_template: str = "sha-{sha}"
    git_identity_name: str = "ci-bot"
    git_identity_email: str = "ci-bot@noreply.local"
    release_branch: str = "main"
    development_branch: str = "dev"
    status_context: str = "knowledge-core/smoke"
    buildkit_reserved_space: str = "2GB"
    buildkit_max_used_space: str = "8GB"
    buildkit_min_free_space: str = "50GB"
    control_image_repository: str = ""
    runner_image_repository: str = ""
    runner_version: str = "2.337.0"
    runner_image_tag: str = "2.337.0"

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Pipeline":
        if not isinstance(value, dict):
            raise ConfigError("pipeline config must be a mapping")
        required = ("project", "source_repo", "gitops_repo", "gitops_path", "gitops_branch", "services")
        missing = [field for field in required if not value.get(field)]
        if missing:
            raise ConfigError(f"pipeline is missing fields: {', '.join(missing)}")
        if not isinstance(value["project"], str) or not value["project"].strip():
            raise ConfigError("project must be a non-empty string")
        for field in ("source_repo", "gitops_repo", "gitops_branch"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ConfigError(f"{field} must be a non-empty string")
        gitops_path = _safe_relative(value["gitops_path"], "gitops_path", allow_dot=False)
        schema_version = value.get("schema_version", 1)
        if not isinstance(schema_version, int) or schema_version not in {1, 2}:
            raise ConfigError("schema_version must be 1 or 2")
        raw_services = value["services"]
        if not isinstance(raw_services, list) or not raw_services:
            raise ConfigError("services must be a non-empty list")
        project_slug = re_slug(value["project"])
        services = tuple(
            Service.from_mapping(item, project_slug, legacy_defaults=schema_version == 1)
            for item in raw_services
        )
        names = [service.name for service in services]
        if len(names) != len(set(names)):
            raise ConfigError("service names must be unique")
        smoke = value.get("smoke_command", [])
        if not isinstance(smoke, list) or not all(isinstance(item, str) and item for item in smoke):
            raise ConfigError("smoke_command must be a list of non-empty strings")
        raw_bases = value.get("base_images", [])
        if not isinstance(raw_bases, list):
            raise ConfigError("base_images must be a list")
        base_images: list[tuple[str, str]] = []
        for item in raw_bases:
            if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not item["source"].strip() or not isinstance(item.get("destination"), str) or not item["destination"].strip():
                raise ConfigError("each base image requires source and destination")
            base_images.append((item["source"], item["destination"]))
        aggregate_prefix = value.get("aggregate_release_prefix", project_slug)
        aggregate_version_file = value.get("aggregate_version_file", "VERSION")
        release_language = value.get("release_language", "en")
        deploy_root = value.get("deploy_root", "deploy")
        if not all(isinstance(item, str) and item.strip() for item in (aggregate_prefix, aggregate_version_file, release_language)):
            raise ConfigError("aggregate release settings must be non-empty strings")
        if not isinstance(deploy_root, str) or not deploy_root.strip() or deploy_root.startswith(("/", "\\")) or ".." in Path(deploy_root).parts:
            raise ConfigError("deploy_root must be a safe relative path")
        gitops_kustomization = _safe_relative(value.get("gitops_kustomization", "kustomization.yaml"), "gitops_kustomization", allow_dot=False)
        shared_paths = value.get("shared_paths", [])
        if not isinstance(shared_paths, list) or not all(isinstance(item, str) and item.strip() for item in shared_paths):
            raise ConfigError("shared_paths must be a list of non-empty relative paths")
        shared_paths = [_safe_relative(item, "shared_paths entry") for item in shared_paths]
        argocd_namespace = value.get("argocd_namespace", "argocd")
        application_suffix = value.get("application_suffix", "-dev")
        smoke_namespace = value.get("smoke_namespace", "")
        for field, field_value in (("argocd_namespace", argocd_namespace), ("application_suffix", application_suffix), ("smoke_namespace", smoke_namespace)):
            if not isinstance(field_value, str) or (field == "argocd_namespace" and not field_value.strip()):
                raise ConfigError(f"{field} must be a string" if field != "argocd_namespace" else "argocd_namespace must be a non-empty string")

        raw_endpoints = value.get("smoke_endpoints", [])
        if not isinstance(raw_endpoints, list):
            raise ConfigError("smoke_endpoints must be a list")
        smoke_endpoints: list[tuple[str, str, str]] = []
        for endpoint in raw_endpoints:
            if not isinstance(endpoint, dict) or not all(isinstance(endpoint.get(key), str) and endpoint[key].strip() for key in ("service", "port", "path")):
                raise ConfigError("each smoke endpoint requires service, port, and path")
            smoke_endpoints.append((endpoint["service"], endpoint["port"], endpoint["path"]))

        raw_smoke_env = value.get("smoke_env", {})
        if not isinstance(raw_smoke_env, dict) or not all(
            isinstance(key, str) and key.strip() and isinstance(item, str) for key, item in raw_smoke_env.items()
        ):
            raise ConfigError("smoke_env must be a mapping of string names to string values")

        status_context = value.get("status_context", f"{project_slug}/smoke")
        if not isinstance(status_context, str) or not status_context.strip():
            raise ConfigError("status_context must be a non-empty string")

        runner_version = value.get("runner_version", "2.337.0")
        string_defaults = {
            "active_image_tag": "dev",
            "previous_image_tag": "previous",
            "cache_image_tag": "buildcache",
            "candidate_tag_template": "sha-{sha}",
            "git_identity_name": "ci-bot",
            "git_identity_email": "ci-bot@noreply.local",
            "release_branch": "main",
            "development_branch": "dev",
            "buildkit_reserved_space": "2GB",
            "buildkit_max_used_space": "8GB",
            "buildkit_min_free_space": "50GB",
            "control_image_repository": "",
            "runner_image_repository": "",
            "runner_version": runner_version,
            "runner_image_tag": runner_version,
        }
        configured_strings = {field: value.get(field, default) for field, default in string_defaults.items()}
        for field, field_value in configured_strings.items():
            if field in {"control_image_repository", "runner_image_repository"} and field_value == "":
                continue
            if not isinstance(field_value, str) or not field_value.strip():
                raise ConfigError(f"{field} must be a non-empty string")
        return cls(
            project=value["project"],
            source_repo=value["source_repo"],
            gitops_repo=value["gitops_repo"],
            gitops_path=gitops_path,
            gitops_kustomization=gitops_kustomization,
            gitops_branch=value["gitops_branch"],
            services=services,
            shared_paths=tuple(shared_paths),
            harbor_registry=value.get("harbor_registry", "harbor.happyladysauce.local"),
            harbor_project=value.get("harbor_project", "knowledge-core"),
            argocd_server=value.get("argocd_server", ""),
            argocd_application=value.get("argocd_application", ""),
            smoke_command=tuple(smoke),
            release_model=value.get("release_model", "git-independent-service"),
            deepseek_model=value.get("deepseek_model", "deepseek-v4-flash"),
            aggregate_release_prefix=aggregate_prefix,
            aggregate_version_file=aggregate_version_file,
            release_language=release_language,
            base_images=tuple(base_images),
            deploy_root=deploy_root,
            schema_version=schema_version,
            argocd_namespace=argocd_namespace,
            application_suffix=application_suffix,
            smoke_namespace=smoke_namespace,
            smoke_endpoints=tuple(smoke_endpoints),
            smoke_env=tuple((key, item) for key, item in raw_smoke_env.items()),
            status_context=status_context,
            **configured_strings,
        )


def load_config(path: str | Path | None = None) -> Pipeline:
    # An explicit file path (including CI_PROJECT_CONFIG) wins over the legacy
    # inline JSON escape hatch. This lets a host retain an old environment
    # variable while projects migrate to checked-in YAML.
    configured_path = os.environ.get("CI_PROJECT_CONFIG") if path is None else None
    raw_config = os.environ.get("CI_PROJECT_CONFIG_JSON") if path is None and not configured_path else None
    if raw_config is not None:
        try:
            data = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid CI_PROJECT_CONFIG_JSON: {exc}") from exc
    else:
        file_path = path or configured_path
        if file_path:
            config_path = Path(file_path)
        else:
            candidates = (Path(".ci/pipeline.yaml"), Path(".ci/pipeline.yml"), Path("ci-pipeline.yaml"), Path("ci-pipeline.json"))
            config_path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
        try:
            text = config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text) if config_path.suffix.lower() in {".yaml", ".yml"} else json.loads(text)
        except OSError as exc:
            raise ConfigError(f"cannot read pipeline config {config_path}: {exc}") from exc
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"invalid YAML/JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("pipeline config must be a mapping")
    return Pipeline.from_mapping(data)


def re_slug(value: str) -> str:
    """Return a stable project slug without embedding an organization name."""
    slug = "-".join(value.strip().lower().split())
    slug = "".join(character if character.isalnum() or character == "-" else "-" for character in slug)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "project"
