from __future__ import annotations

import json
import math
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


def _builder_state_path() -> Path:
    root = os.environ.get("BUILDX_CONFIG", "").strip()
    if root:
        return Path(root) / _BUILDER_STATE_FILE
    return Path.home() / ".docker" / "buildx" / _BUILDER_STATE_FILE


def _builder_matches(jobs: int, registry: str) -> bool:
    try:
        state = json.loads(_builder_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return state == {"jobs": jobs, "registry": registry}


def _write_builder_state(jobs: int, registry: str) -> None:
    path = _builder_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": jobs, "registry": registry}) + "\n", encoding="utf-8")
    except OSError:
        # The builder remains usable when the optional marker cannot be stored.
        pass


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
    registry = service.image_repository.split("/", 1)[0]
    if inspect.returncode == 0 and _builder_matches(jobs, registry):
        return STABLE_BUILDER

    if inspect.returncode == 0:
        _docker(["buildx", "rm", "--force", STABLE_BUILDER], cwd=cwd)

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
        buildkit_config = tempfile.TemporaryDirectory(prefix="ci-templates-buildkit-")
        config_path = Path(buildkit_config.name) / "buildkitd.toml"
        config_path.write_text(
            _buildkitd_config(jobs, registry, str(Path(registry_ca)) if registry_ca else None),
            encoding="utf-8",
        )
        create_args.extend(["--buildkitd-config", str(config_path)])
        _docker(create_args, cwd=cwd)
        _write_builder_state(jobs, registry)
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
