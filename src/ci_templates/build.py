from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .config import Service
from .harbor import HarborClient, ImageRef


class BuildError(RuntimeError):
    pass


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
STABLE_BUILDER = "ci-templates"


def build_jobs() -> int:
    """Use at most three CPUs, respecting the caller's affinity mask."""
    try:
        available = len(os.sched_getaffinity(0))
    except AttributeError:
        available = os.cpu_count() or 1
    return max(1, min(3, available))


def _docker(args: list[str], cwd: str = ".", check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], cwd=cwd, check=check, stdout=sys.stderr, text=True)


def _buildkitd_config(jobs: int, registry: str, registry_ca: str | None) -> str:
    lines = [
        "[worker.oci]",
        f"  max-parallelism = {jobs}",
        "  gc = true",
        '  reservedSpace = "2GB"',
        '  maxUsedSpace = "8GB"',
        '  minFreeSpace = "50GB"',
    ]
    if registry_ca:
        lines.extend(
            [
                "",
                f"[registry.{json.dumps(registry)}]",
                f"  ca = [{json.dumps(registry_ca)}]",
            ]
        )
    return "\n".join(lines) + "\n"


def _ensure_builder(service: Service, jobs: int, cwd: str) -> str:
    inspect = _docker(["buildx", "inspect", STABLE_BUILDER], cwd=cwd, check=False)
    if inspect.returncode == 0:
        return STABLE_BUILDER

    # The runner reaches Harbor through its host DNS/network.  BuildKit runs in
    # a container, so keep that container on host networking as well; otherwise
    # private registry names resolve on the host but not inside BuildKit.
    create_args = [
        "buildx", "create", "--driver", "docker-container",
        "--driver-opt", "network=host", "--name", STABLE_BUILDER,
    ]
    registry_ca = os.environ.get("CI_REGISTRY_CA_FILE", "").strip()
    buildkit_config: tempfile.TemporaryDirectory[str] | None = None
    try:
        if registry_ca:
            ca_path = Path(registry_ca)
            if not ca_path.is_file():
                raise BuildError("registry CA file is unavailable")
        registry = service.image_repository.split("/", 1)[0]
        buildkit_config = tempfile.TemporaryDirectory(prefix="ci-templates-buildkit-")
        config_path = Path(buildkit_config.name) / "buildkitd.toml"
        config_path.write_text(
            _buildkitd_config(jobs, registry, str(Path(registry_ca)) if registry_ca else None),
            encoding="utf-8",
        )
        create_args.extend(["--buildkitd-config", str(config_path)])
        _docker(create_args, cwd=cwd)
    except Exception:
        _docker(["buildx", "rm", "--force", STABLE_BUILDER], cwd=cwd, check=False)
        raise
    finally:
        if buildkit_config is not None:
            buildkit_config.cleanup()
    return STABLE_BUILDER


def _validate_artifact_manifest(service: Service, manifest: str | None, cwd: str) -> None:
    if not manifest:
        return
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
        entry = payload["services"][service.name]
        relative = Path(str(entry["path"]))
        expected = str(entry["sha256"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid artifact manifest for {service.name}") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise BuildError(f"artifact path escapes workspace for {service.name}")
    artifact = Path(cwd) / relative
    if not artifact.is_file():
        raise BuildError(f"artifact is missing for {service.name}: {artifact}")
    import hashlib

    actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual != expected:
        raise BuildError(f"artifact digest mismatch for {service.name}")


def build_service(
    service: Service,
    tag: str = "dev",
    cwd: str = ".",
    *,
    preserve_previous: bool = True,
    artifact_manifest: str | None = None,
) -> str:
    image = f"{service.image_repository}:{tag}"
    cache = f"{service.image_repository}:buildcache"
    jobs = build_jobs()
    _validate_artifact_manifest(service, artifact_manifest, cwd)
    try:
        if preserve_previous:
            current = _docker(["pull", f"{service.image_repository}:dev"], cwd=cwd, check=False)
            if current.returncode == 0:
                _docker(["tag", f"{service.image_repository}:dev", f"{service.image_repository}:previous"], cwd=cwd)
                _docker(["push", f"{service.image_repository}:previous"], cwd=cwd)
        builder = _ensure_builder(service, jobs, cwd)
        _docker([
            "buildx", "build", "--builder", builder, "--push", "--provenance=false", "--sbom=false",
            "--file", service.dockerfile, "--tag", image,
            "--build-arg", f"BUILD_JOBS={jobs}",
            "--cache-from", f"type=registry,ref={cache}",
            "--cache-to", f"type=registry,ref={cache},mode=max",
            service.context,
        ], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"image build failed for {service.name}") from exc
    return image


def promote_candidate(service: Service, candidate_tag: str, cwd: str = ".") -> None:
    """Promote a registry candidate without loading it into the local daemon."""
    source = f"{service.image_repository}:{candidate_tag}"
    target = f"{service.image_repository}:dev"
    try:
        _docker(["buildx", "imagetools", "create", "--tag", target, source], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"cannot promote candidate for {service.name}") from exc


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
