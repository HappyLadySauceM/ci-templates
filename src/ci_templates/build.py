from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from .config import Service
from .harbor import HarborClient, ImageRef


class BuildError(RuntimeError):
    pass


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _docker(args: list[str], cwd: str = ".", check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], cwd=cwd, check=check, stdout=sys.stderr, text=True)


def build_service(service: Service, tag: str = "dev", cwd: str = ".") -> str:
    image = f"{service.image_repository}:{tag}"
    cache = f"{service.image_repository}:buildcache"
    builder = f"ci-templates-{service.name}-{uuid.uuid4().hex[:12]}"
    buildkit_config: tempfile.TemporaryDirectory[str] | None = None
    try:
        current = _docker(["pull", f"{service.image_repository}:dev"], cwd=cwd, check=False)
        if current.returncode == 0:
            _docker(["tag", f"{service.image_repository}:dev", f"{service.image_repository}:previous"], cwd=cwd)
            _docker(["push", f"{service.image_repository}:previous"], cwd=cwd)
        create_args = ["buildx", "create", "--driver", "docker-container", "--name", builder]
        registry_ca = os.environ.get("CI_REGISTRY_CA_FILE", "").strip()
        if registry_ca:
            ca_path = Path(registry_ca)
            if not ca_path.is_file():
                raise BuildError("registry CA file is unavailable")
            registry = service.image_repository.split("/", 1)[0]
            buildkit_config = tempfile.TemporaryDirectory(prefix="ci-templates-buildkit-")
            config_path = Path(buildkit_config.name) / "buildkitd.toml"
            config_path.write_text(
                f"[registry.{json.dumps(registry)}]\n  ca = [{json.dumps(str(ca_path))}]\n",
                encoding="utf-8",
            )
            create_args.extend(["--buildkitd-config", str(config_path)])
        _docker(create_args, cwd=cwd)
        _docker([
            "buildx", "build", "--builder", builder, "--push", "--provenance=false", "--sbom=false",
            "--file", service.dockerfile, "--tag", image,
            "--cache-from", f"type=registry,ref={cache}",
            "--cache-to", f"type=registry,ref={cache},mode=max",
            service.context,
        ], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"image build failed for {service.name}") from exc
    finally:
        _docker(["buildx", "rm", "--force", builder], cwd=cwd, check=False)
        if buildkit_config is not None:
            buildkit_config.cleanup()
    return image


def image_digest(image: str, cwd: str = ".") -> str:
    try:
        result = subprocess.run(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{.Manifest.Digest}}"], cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"cannot inspect digest for {image}") from exc
    for line in reversed(result.stdout.splitlines()):
        value = line.strip()
        if _DIGEST_RE.fullmatch(value):
            return value
        if value.startswith("Digest:"):
            digest = value.removeprefix("Digest:").strip()
            if _DIGEST_RE.fullmatch(digest):
                return digest
    raise BuildError(f"cannot resolve digest for {image}")


def discard_previous(service: Service, cwd: str = ".") -> None:
    _docker(["push", f"{service.image_repository}:dev"], cwd=cwd)
    _docker(["rmi", f"{service.image_repository}:previous"], cwd=cwd, check=False)


def delete_previous(service: Service, registry: str) -> None:
    HarborClient(registry).delete_tag(ImageRef.parse(f"{service.image_repository}:previous"))


def restore_previous(service: Service, cwd: str = ".") -> None:
    try:
        _docker(["pull", f"{service.image_repository}:previous"], cwd=cwd)
        _docker(["tag", f"{service.image_repository}:previous", f"{service.image_repository}:dev"], cwd=cwd)
        _docker(["push", f"{service.image_repository}:dev"], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"cannot restore previous image for {service.name}") from exc


def prewarm_base_images(base_images: tuple[tuple[str, str], ...], cwd: str = ".") -> None:
    for source, destination in base_images:
        try:
            _docker(["pull", source], cwd=cwd)
            _docker(["tag", source, destination], cwd=cwd)
            _docker(["push", destination], cwd=cwd)
        except subprocess.CalledProcessError as exc:
            raise BuildError(f"base image prewarm failed for {source}") from exc
