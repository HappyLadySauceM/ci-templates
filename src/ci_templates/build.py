from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .config import Service
from .harbor import HarborClient, HarborError, ImageRef


class BuildError(RuntimeError):
    pass


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
DEFAULT_BUILDER_NAME = "ci-templates"
# Buildx names are also used as Docker object names and as marker filenames.
# Keep the accepted alphabet deliberately narrow so an environment variable
# cannot escape the marker directory or alter the command shape.
_BUILDER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")
STABLE_BUILDER = DEFAULT_BUILDER_NAME
DEFAULT_CPU_PERCENT = 75
_BUILDER_STATE_FILE = "ci-templates-resource.json"


def _cpu_percent() -> int:
    value = os.environ.get("BUILD_CPU_PERCENT", str(DEFAULT_CPU_PERCENT)).strip()
    try:
        percent = int(value)
    except ValueError as exc:
        raise BuildError("BUILD_CPU_PERCENT must be an integer from 1 to 100") from exc
    if not 1 <= percent <= 100:
        raise BuildError("BUILD_CPU_PERCENT must be an integer from 1 to 100")
    return percent


def _affinity_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def _quota_cpus() -> int | None:
    """Return the integer CPU quota when the current cgroup exposes one."""
    candidates = (
        Path("/sys/fs/cgroup/cpu.max"),
        Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
    )
    for path in candidates:
        try:
            fields = path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if path.name == "cpu.max":
            if len(fields) < 2 or fields[0] == "max":
                return None
            quota, period = fields[0], fields[1]
        else:
            try:
                period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(encoding="utf-8").strip()
            except OSError:
                return None
            quota = fields[0] if fields else "-1"
            if quota == "-1":
                return None
        try:
            quota_value = float(quota)
            period_value = float(period)
        except ValueError:
            return None
        if quota_value <= 0 or period_value <= 0:
            return None
        return max(1, math.floor(quota_value / period_value))
    return None


def available_cpus() -> int:
    affinity = _affinity_cpus()
    quota = _quota_cpus()
    return max(1, min(affinity, quota) if quota is not None else affinity)


def build_jobs() -> int:
    """Use a CPU percentage, respecting affinity and cgroup quotas.

    BUILD_JOBS is an optional emergency override, still bounded by available
    CPUs. BUILD_CPU_PERCENT is the normal, ratio-based configuration.
    """
    available = available_cpus()
    override = os.environ.get("BUILD_JOBS", "").strip()
    if override:
        try:
            requested = int(override)
        except ValueError as exc:
            raise BuildError("BUILD_JOBS must be a positive integer") from exc
        if requested < 1:
            raise BuildError("BUILD_JOBS must be a positive integer")
        return min(requested, available)
    return max(1, available * _cpu_percent() // 100)


def _validated_builder_name(value: str) -> str:
    if not _BUILDER_NAME_RE.fullmatch(value):
        raise BuildError("CI_BUILDER_NAME must contain 1-63 letters, digits, '.', '_' or '-'")
    return value


def _configured_builder_name() -> str:
    value = os.environ.get("CI_BUILDER_NAME", DEFAULT_BUILDER_NAME)
    return _validated_builder_name(value)


def _builder_state_path(builder_name: str | None = None) -> Path:
    name = _configured_builder_name() if builder_name is None else _validated_builder_name(builder_name)
    # Keep the historical default marker path so existing runners reuse their
    # ci-templates builder after upgrading. Custom names get separate markers.
    filename = _BUILDER_STATE_FILE if name == DEFAULT_BUILDER_NAME else f"{name}-resource.json"
    root = os.environ.get("BUILDX_CONFIG", "").strip()
    if root:
        return Path(root) / filename
    return Path.home() / ".docker" / "buildx" / filename


def _builder_matches(
    jobs: int,
    registry: str,
    *,
    builder_name: str | None = None,
    registry_ca_fingerprint: str | None = None,
) -> bool:
    name = _configured_builder_name() if builder_name is None else _validated_builder_name(builder_name)
    try:
        state = json.loads(_builder_state_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return state == {
        "builder": name,
        "jobs": jobs,
        "registry": registry,
        "registry_ca_sha256": registry_ca_fingerprint,
    }


def _write_builder_state(
    jobs: int,
    registry: str,
    *,
    builder_name: str | None = None,
    registry_ca_fingerprint: str | None = None,
) -> None:
    name = _configured_builder_name() if builder_name is None else _validated_builder_name(builder_name)
    path = _builder_state_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "builder": name,
                    "jobs": jobs,
                    "registry": registry,
                    "registry_ca_sha256": registry_ca_fingerprint,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        # The builder remains usable when the optional marker cannot be stored.
        pass


def _docker(args: list[str], cwd: str = ".", check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], cwd=cwd, check=check, stdout=sys.stderr, text=True)


def _buildkitd_config(
    jobs: int,
    registry: str,
    registry_ca: str | None,
    reserved_space: str | None = None,
    max_used_space: str | None = None,
    min_free_space: str | None = None,
) -> str:
    reserved = (reserved_space if reserved_space is not None else os.environ.get("BUILDKIT_RESERVED_SPACE", "2GB")).strip() or "2GB"
    maximum = (max_used_space if max_used_space is not None else os.environ.get("BUILDKIT_MAX_USED_SPACE", "8GB")).strip() or "8GB"
    minimum = (min_free_space if min_free_space is not None else os.environ.get("BUILDKIT_MIN_FREE_SPACE", "50GB")).strip() or "50GB"
    lines = [
        "[worker.oci]",
        f"  max-parallelism = {jobs}",
        "  gc = true",
        f"  reservedSpace = {json.dumps(reserved)}",
        f"  maxUsedSpace = {json.dumps(maximum)}",
        f"  minFreeSpace = {json.dumps(minimum)}",
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


def _registry_ca_fingerprint(registry_ca: str | None) -> str | None:
    if not registry_ca:
        return None
    ca_path = Path(registry_ca)
    if not ca_path.is_file():
        raise BuildError("registry CA file is unavailable")
    digest = hashlib.sha256()
    try:
        with ca_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BuildError("registry CA file is unavailable") from exc
    return digest.hexdigest()


def _ensure_builder(
    service: Service,
    jobs: int,
    cwd: str,
    *,
    reserved_space: str | None = None,
    max_used_space: str | None = None,
    min_free_space: str | None = None,
) -> str:
    name = _configured_builder_name()
    registry = service.image_repository.split("/", 1)[0]
    registry_ca = os.environ.get("CI_REGISTRY_CA_FILE", "").strip() or None
    registry_ca_fingerprint = _registry_ca_fingerprint(registry_ca)
    inspect = _docker(["buildx", "inspect", name], cwd=cwd, check=False)
    if inspect.returncode == 0 and _builder_matches(
        jobs,
        registry,
        builder_name=name,
        registry_ca_fingerprint=registry_ca_fingerprint,
    ):
        return name

    if inspect.returncode == 0:
        _docker(["buildx", "rm", "--force", name], cwd=cwd)

    # The runner reaches Harbor through its host DNS/network.  BuildKit runs in
    # a container, so keep that container on host networking as well; otherwise
    # private registry names resolve on the host but not inside BuildKit.
    create_args = [
        "buildx", "create", "--driver", "docker-container",
        "--driver-opt", f"network={os.environ.get('BUILDKIT_NETWORK', 'host')}", "--name", name,
    ]
    buildkit_config: tempfile.TemporaryDirectory[str] | None = None
    try:
        buildkit_config = tempfile.TemporaryDirectory(prefix="ci-templates-buildkit-")
        config_path = Path(buildkit_config.name) / "buildkitd.toml"
        config_path.write_text(
            _buildkitd_config(
                jobs,
                registry,
                str(Path(registry_ca)) if registry_ca else None,
                reserved_space,
                max_used_space,
                min_free_space,
            ),
            encoding="utf-8",
        )
        create_args.extend(["--buildkitd-config", str(config_path)])
        _docker(create_args, cwd=cwd)
        _write_builder_state(
            jobs,
            registry,
            builder_name=name,
            registry_ca_fingerprint=registry_ca_fingerprint,
        )
    except Exception:
        _docker(["buildx", "rm", "--force", name], cwd=cwd, check=False)
        raise
    finally:
        if buildkit_config is not None:
            buildkit_config.cleanup()
    return name


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
    reuse_existing: bool = False,
    active_tag: str = "dev",
    previous_tag: str = "previous",
    cache_tag: str = "buildcache",
    buildkit_reserved_space: str | None = None,
    buildkit_max_used_space: str | None = None,
    buildkit_min_free_space: str | None = None,
) -> str:
    # Validate the builder selector before any pull/tag/push side effects.
    _configured_builder_name()
    image = f"{service.image_repository}:{tag}"
    cache = f"{service.image_repository}:{cache_tag}"
    jobs = build_jobs()
    _validate_artifact_manifest(service, artifact_manifest, cwd)
    try:
        # Candidate tags are content-addressed by source SHA and immutable in
        # Harbor. Reusing an existing candidate makes workflow retries
        # idempotent without rebuilding or attempting to overwrite the tag.
        if reuse_existing:
            try:
                image_digest(image, cwd=cwd)
                return image
            except BuildError:
                pass
        if preserve_previous:
            current = _docker(["pull", f"{service.image_repository}:{active_tag}"], cwd=cwd, check=False)
            if current.returncode == 0:
                _docker(["tag", f"{service.image_repository}:{active_tag}", f"{service.image_repository}:{previous_tag}"], cwd=cwd)
                _docker(["push", f"{service.image_repository}:{previous_tag}"], cwd=cwd)
        builder = _ensure_builder(
            service,
            jobs,
            cwd,
            reserved_space=buildkit_reserved_space,
            max_used_space=buildkit_max_used_space,
            min_free_space=buildkit_min_free_space,
        )
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
    """Backward-compatible wrapper for Harbor API tag promotion.

    The ``cwd`` argument is retained for callers of the old helper, but image
    promotion deliberately never talks to a local Docker daemon.
    """
    del cwd
    source = ImageRef.parse(f"{service.image_repository}:{candidate_tag}")
    target = ImageRef.parse(f"{service.image_repository}:dev")
    try:
        HarborClient(source.registry).promote_tag(source, target)
    except HarborError as exc:
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


def discard_previous(service: Service, cwd: str = ".", active_tag: str = "dev", previous_tag: str = "previous") -> None:
    _docker(["push", f"{service.image_repository}:{active_tag}"], cwd=cwd)
    _docker(["rmi", f"{service.image_repository}:{previous_tag}"], cwd=cwd, check=False)


def delete_previous(service: Service, registry: str, previous_tag: str = "previous") -> None:
    HarborClient(registry).delete_tag(ImageRef.parse(f"{service.image_repository}:{previous_tag}"))


def restore_previous(service: Service, cwd: str = ".", active_tag: str = "dev", previous_tag: str = "previous") -> None:
    try:
        _docker(["pull", f"{service.image_repository}:{previous_tag}"], cwd=cwd)
        _docker(["tag", f"{service.image_repository}:{previous_tag}", f"{service.image_repository}:{active_tag}"], cwd=cwd)
        _docker(["push", f"{service.image_repository}:{active_tag}"], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"cannot restore previous image for {service.name}") from exc


def prewarm_base_images(base_images: tuple[tuple[str, str], ...], cwd: str = ".") -> None:
    for source, destination in base_images:
        try:
            # The destination is a shared cache tag. Reusing it when present
            # keeps repeated or concurrent workflows from trying to overwrite
            # an immutable Harbor tag.
            try:
                image_digest(destination, cwd=cwd)
                continue
            except BuildError:
                pass
            _docker(["pull", source], cwd=cwd)
            _docker(["tag", source, destination], cwd=cwd)
            _docker(["push", destination], cwd=cwd)
        except subprocess.CalledProcessError as exc:
            raise BuildError(f"base image prewarm failed for {source}") from exc
