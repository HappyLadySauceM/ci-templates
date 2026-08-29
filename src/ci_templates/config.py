from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a pipeline configuration is incomplete or unsafe."""


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

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Service":
        fields = ("name", "source_path", "version_file", "dockerfile", "context", "image_repository", "deploy_snapshot")
        missing = [field for field in fields if not isinstance(value.get(field), str) or not value[field].strip()]
        if missing:
            raise ConfigError(f"service is missing fields: {', '.join(missing)}")
        if any(part in value["name"] for part in ("/", "\\", "..")):
            raise ConfigError(f"invalid service name: {value['name']!r}")
        return cls(**{field: value[field] for field in fields}, kustomize_name=value.get("kustomize_name", f"knowledge-core-{value['name']}"))


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

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Pipeline":
        required = ("project", "source_repo", "gitops_repo", "gitops_path", "gitops_branch", "services")
        missing = [field for field in required if not value.get(field)]
        if missing:
            raise ConfigError(f"pipeline is missing fields: {', '.join(missing)}")
        if not isinstance(value["project"], str) or not value["project"].strip():
            raise ConfigError("project must be a non-empty string")
        raw_services = value["services"]
        if not isinstance(raw_services, list) or not raw_services:
            raise ConfigError("services must be a non-empty list")
        services = tuple(Service.from_mapping(item) for item in raw_services)
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
            if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("destination"), str):
                raise ConfigError("each base image requires source and destination")
            base_images.append((item["source"], item["destination"]))
        project_slug = value["project"].strip().lower().replace(" ", "-")
        aggregate_prefix = value.get("aggregate_release_prefix", project_slug)
        aggregate_version_file = value.get("aggregate_version_file", "VERSION")
        release_language = value.get("release_language", "en")
        deploy_root = value.get("deploy_root", "deploy")
        if not all(isinstance(item, str) and item.strip() for item in (aggregate_prefix, aggregate_version_file, release_language)):
            raise ConfigError("aggregate release settings must be non-empty strings")
        if not isinstance(deploy_root, str) or not deploy_root.strip() or deploy_root.startswith(("/", "\\")) or ".." in Path(deploy_root).parts:
            raise ConfigError("deploy_root must be a safe relative path")
        return cls(
            project=value["project"],
            source_repo=value["source_repo"],
            gitops_repo=value["gitops_repo"],
            gitops_path=value["gitops_path"],
            gitops_kustomization=value.get("gitops_kustomization", "kustomization.yaml"),
            gitops_branch=value["gitops_branch"],
            services=services,
            shared_paths=tuple(value.get("shared_paths", [])),
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
        )


def load_config(path: str | Path | None = None) -> Pipeline:
    raw_config = os.environ.get("CI_PROJECT_CONFIG_JSON") if path is None else None
    if raw_config is not None:
        try:
            data = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid CI_PROJECT_CONFIG_JSON: {exc}") from exc
    else:
        config_path = Path(path or os.environ.get("CI_PROJECT_CONFIG", "ci-pipeline.json"))
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read pipeline config {config_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("pipeline config must be a JSON object")
    return Pipeline.from_mapping(data)
