from __future__ import annotations

import subprocess

from .config import Service
from .harbor import HarborClient, ImageRef


class BuildError(RuntimeError):
    pass


def _docker(args: list[str], cwd: str = ".", check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], cwd=cwd, check=check, text=True)


def build_service(service: Service, tag: str = "dev", cwd: str = ".") -> str:
    image = f"{service.image_repository}:{tag}"
    cache = f"{service.image_repository}:buildcache"
    try:
        current = _docker(["pull", f"{service.image_repository}:dev"], cwd=cwd, check=False)
        if current.returncode == 0:
            _docker(["tag", f"{service.image_repository}:dev", f"{service.image_repository}:previous"], cwd=cwd)
            _docker(["push", f"{service.image_repository}:previous"], cwd=cwd)
        _docker([
            "buildx", "build", "--push", "--provenance=false", "--sbom=false",
            "--file", service.dockerfile, "--tag", image,
            "--cache-from", f"type=registry,ref={cache}",
            "--cache-to", f"type=registry,ref={cache},mode=max",
            service.context,
        ], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"image build failed for {service.name}") from exc
    return image


def image_digest(image: str, cwd: str = ".") -> str:
    result = subprocess.run(["docker", "buildx", "imagetools", "inspect", image, "--format", "{{.Manifest.Digest}}"], cwd=cwd, check=True, capture_output=True, text=True)
    digest = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not digest.startswith("sha256:"):
        raise BuildError(f"cannot resolve digest for {image}")
    return digest


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
