from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

import yaml


class ChartError(RuntimeError):
    """Raised when a chart cannot be verified or mirrored safely."""


class KubernetesLoader(yaml.SafeLoader):
    pass


def _construct_yaml_value(loader: KubernetesLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


KubernetesLoader.add_constructor("tag:yaml.org,2002:value", _construct_yaml_value)


@dataclass(frozen=True)
class Chart:
    name: str
    repository: str
    version: str
    sha256: str
    target_version: str
    release_name: str
    namespace: str
    values: tuple[str, ...]
    include_crds: bool
    remove_templates: tuple[str, ...]
    replace_templates: tuple[tuple[str, str], ...]
    vendor_path: str | None
    source_name: str | None = None


@dataclass(frozen=True)
class ChartManifest:
    destination: str
    charts: tuple[Chart, ...]


def _required_string(value: dict[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ChartError(f"{context} requires a non-empty {key}")
    return result


def _relative_path(value: str, context: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ChartError(f"{context} must be a safe relative path: {value!r}")
    return path.as_posix()


def load_chart_manifest(path: str | Path) -> ChartManifest:
    manifest_path = Path(path)
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChartError(f"cannot read chart manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ChartError("chart manifest version must be 1")
    destination = _required_string(raw, "destination", "chart manifest")
    if not destination.startswith("oci://"):
        raise ChartError("chart manifest destination must use oci://")
    raw_charts = raw.get("charts")
    if not isinstance(raw_charts, list) or not raw_charts:
        raise ChartError("chart manifest charts must be a non-empty list")

    charts: list[Chart] = []
    for index, item in enumerate(raw_charts):
        context = f"charts[{index}]"
        if not isinstance(item, dict):
            raise ChartError(f"{context} must be a mapping")
        name = _required_string(item, "name", context)
        source_name = item.get("sourceName", name)
        if not isinstance(source_name, str) or not source_name.strip() or "/" in source_name or "\\" in source_name:
            raise ChartError(f"{context}.sourceName must be a chart name")
        digest = _required_string(item, "sha256", context).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ChartError(f"{context}.sha256 must be a lowercase SHA256 digest")
        values = item.get("values", [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ChartError(f"{context}.values must be a list of paths")
        remove_templates = item.get("removeTemplates", [])
        if not isinstance(remove_templates, list) or not all(isinstance(value, str) for value in remove_templates):
            raise ChartError(f"{context}.removeTemplates must be a list of paths")
        replace_templates = item.get("replaceTemplates", {})
        if not isinstance(replace_templates, dict) or not all(
            isinstance(target, str) and isinstance(source, str)
            for target, source in replace_templates.items()
        ):
            raise ChartError(f"{context}.replaceTemplates must map chart paths to repository paths")
        vendor_path = item.get("vendorPath")
        if vendor_path is not None and not isinstance(vendor_path, str):
            raise ChartError(f"{context}.vendorPath must be a path")
        charts.append(Chart(
            name=name,
            repository=_required_string(item, "repository", context),
            version=_required_string(item, "version", context),
            sha256=digest,
            target_version=_required_string(item, "targetVersion", context),
            release_name=_required_string(item, "releaseName", context),
            namespace=_required_string(item, "namespace", context),
            values=tuple(_relative_path(value, f"{context}.values") for value in values),
            include_crds=item.get("includeCRDs", False) is True,
            remove_templates=tuple(_relative_path(value, f"{context}.removeTemplates") for value in remove_templates),
            replace_templates=tuple(
                (
                    _relative_path(target, f"{context}.replaceTemplates target"),
                    _relative_path(source, f"{context}.replaceTemplates source"),
                )
                for target, source in replace_templates.items()
            ),
            vendor_path=_relative_path(vendor_path, f"{context}.vendorPath") if vendor_path else None,
            source_name=source_name if source_name != name else None,
        ))
    names = [chart.name for chart in charts]
    if len(names) != len(set(names)):
        raise ChartError("chart names must be unique")
    return ChartManifest(destination=destination.rstrip("/"), charts=tuple(charts))


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = "\n".join(part for part in (exc.stdout, exc.stderr) if part).strip()
        if detail:
            detail = " | ".join(detail.splitlines()[-8:])
        raise ChartError(f"command failed: {' '.join(args)}{': ' + detail if detail else ''}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_chart(archive: Path, target: Path) -> Path:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            for member in members:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ChartError(f"chart archive contains unsafe path: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise ChartError(f"chart archive contains unsupported entry: {member.name}")
            bundle.extractall(target, members=members)
    except (OSError, tarfile.TarError) as exc:
        raise ChartError(f"cannot extract chart archive {archive}: {exc}") from exc
    roots = [entry for entry in target.iterdir() if entry.is_dir()]
    if len(roots) != 1 or not (roots[0] / "Chart.yaml").is_file():
        raise ChartError("chart archive must contain one chart directory")
    return roots[0]


def _set_chart_version(chart_dir: Path, version: str) -> None:
    metadata_path = chart_dir / "Chart.yaml"
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChartError(f"cannot read {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise ChartError(f"invalid chart metadata in {metadata_path}")
    metadata["version"] = version
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8")


def _remove_templates(chart_dir: Path, paths: tuple[str, ...]) -> None:
    chart_root = chart_dir.resolve()
    for value in paths:
        target = (chart_dir / value).resolve()
        if chart_root not in target.parents or not target.is_file():
            raise ChartError(f"removeTemplates entry is not a chart file: {value}")
        target.write_text("{{- /* Resource is supplied by the deployment secret manager. */ -}}\n", encoding="utf-8")


def _replace_templates(chart_dir: Path, root: Path, replacements: tuple[tuple[str, str], ...]) -> None:
    chart_root = chart_dir.resolve()
    repository_root = root.resolve()
    for target_value, source_value in replacements:
        target = (chart_dir / target_value).resolve()
        source = (root / source_value).resolve()
        if chart_root not in target.parents or not target.is_file():
            raise ChartError(f"replaceTemplates target is not a chart file: {target_value}")
        if repository_root not in source.parents or not source.is_file():
            raise ChartError(f"replaceTemplates source is not a repository file: {source_value}")
        shutil.copyfile(source, target)


def _resolve_values(root: Path, chart: Chart) -> list[Path]:
    values: list[Path] = []
    root_resolved = root.resolve()
    for value in chart.values:
        path = (root / value).resolve()
        if root_resolved not in path.parents or not path.is_file():
            raise ChartError(f"values file does not exist under repository root: {value}")
        values.append(path)
    return values


def _render(chart_dir: Path, chart: Chart, values: list[Path]) -> bytes:
    args = ["helm", "template", chart.release_name, str(chart_dir), "--namespace", chart.namespace]
    if chart.include_crds:
        args.append("--include-crds")
    for path in values:
        args.extend(["--values", str(path)])
    return _run(args).stdout.encode("utf-8")


def _resource_identity(resource: dict[str, Any]) -> str:
    api_version = resource.get("apiVersion")
    kind = resource.get("kind")
    metadata = resource.get("metadata")
    if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(metadata, dict):
        raise ChartError("rendered document is not a Kubernetes resource")
    name = metadata.get("name")
    namespace = metadata.get("namespace", "")
    if not isinstance(name, str) or not name or not isinstance(namespace, str):
        raise ChartError("rendered Kubernetes resource has invalid metadata")
    return f"{api_version}|{kind}|{namespace}|{name}"


def _validate_rendered(chart: Chart, rendered: bytes) -> tuple[str, ...]:
    try:
        documents = yaml.load_all(rendered.decode("utf-8"), Loader=KubernetesLoader)
        identities: list[str] = []
        for document in documents:
            if document is None:
                continue
            if not isinstance(document, dict):
                raise ChartError(f"chart {chart.name} rendered a non-mapping YAML document")
            if document.get("kind") == "Secret":
                metadata = document.get("metadata", {})
                raise ChartError(f"chart {chart.name} rendered forbidden Secret {metadata.get('name', '<unknown>')}")
            identities.append(_resource_identity(document))
    except yaml.YAMLError as exc:
        raise ChartError(f"chart {chart.name} rendered invalid YAML: {exc}") from exc
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        raise ChartError(f"chart {chart.name} rendered duplicate resources: {', '.join(duplicates)}")
    if not identities:
        raise ChartError(f"chart {chart.name} rendered no resources")
    return tuple(identities)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _prepare_chart(chart: Chart, root: Path, work: Path) -> tuple[Path, tuple[str, ...]]:
    download_dir = work / "download"
    extract_dir = work / "extract"
    download_dir.mkdir()
    extract_dir.mkdir()
    if chart.repository.startswith("oci://"):
        source = f"{chart.repository.rstrip('/')}/{chart.source_name or chart.name}"
        _run([
            "helm", "pull", source, "--version", chart.version,
            "--destination", str(download_dir),
        ])
    else:
        _run([
            "helm", "pull", chart.source_name or chart.name, "--repo", chart.repository,
            "--version", chart.version, "--destination", str(download_dir),
        ])
    archives = list(download_dir.glob("*.tgz"))
    if len(archives) != 1:
        raise ChartError(f"expected one downloaded archive for chart {chart.name}")
    actual_digest = _sha256(archives[0])
    if actual_digest != chart.sha256:
        raise ChartError(f"chart {chart.name} digest mismatch: expected {chart.sha256}, got {actual_digest}")
    chart_dir = _extract_chart(archives[0], extract_dir)
    _remove_templates(chart_dir, chart.remove_templates)
    _replace_templates(chart_dir, root, chart.replace_templates)
    _set_chart_version(chart_dir, chart.target_version)
    values = _resolve_values(root, chart)
    _run(["helm", "lint", str(chart_dir), *sum((["--values", str(path)] for path in values), [])])
    first_render = _render(chart_dir, chart, values)
    second_render = _render(chart_dir, chart, values)
    if first_render != second_render:
        raise ChartError(f"chart {chart.name} rendering is not deterministic")
    identities = _validate_rendered(chart, first_render)
    return chart_dir, identities


def _replace_vendor(chart_dir: Path, root: Path, vendor_path: str) -> None:
    root_resolved = root.resolve()
    destination = (root / vendor_path).resolve()
    if root_resolved not in destination.parents:
        raise ChartError(f"vendor path escapes repository root: {vendor_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.new"
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(chart_dir, temporary)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(temporary, destination)


def _check_vendor(chart_dir: Path, root: Path, vendor_path: str) -> None:
    destination = root / vendor_path
    if not destination.is_dir():
        raise ChartError(f"vendored chart is missing: {vendor_path}")
    if _tree_digest(chart_dir) != _tree_digest(destination):
        raise ChartError(f"vendored chart differs from locked source: {vendor_path}")


def check_charts(manifest_path: str | Path, root: str | Path = ".", require_vendors: bool = True) -> dict[str, Any]:
    manifest = load_chart_manifest(manifest_path)
    repository_root = Path(root).resolve()
    checked: list[dict[str, Any]] = []
    for chart in manifest.charts:
        with tempfile.TemporaryDirectory(prefix=f"chart-{chart.name}-") as directory:
            chart_dir, identities = _prepare_chart(chart, repository_root, Path(directory))
            if require_vendors and chart.vendor_path:
                _check_vendor(chart_dir, repository_root, chart.vendor_path)
            checked.append({"name": chart.name, "version": chart.target_version, "resources": len(identities)})
    return {"destination": manifest.destination, "charts": checked}


def mirror_charts(manifest_path: str | Path, root: str | Path = ".") -> dict[str, Any]:
    manifest = load_chart_manifest(manifest_path)
    repository_root = Path(root).resolve()
    mirrored: list[dict[str, Any]] = []
    pushed: set[tuple[object, ...]] = set()
    for chart in manifest.charts:
        with tempfile.TemporaryDirectory(prefix=f"chart-{chart.name}-") as directory:
            work = Path(directory)
            chart_dir, identities = _prepare_chart(chart, repository_root, work)
            package_dir = work / "package"
            package_dir.mkdir()
            _run(["helm", "package", str(chart_dir), "--destination", str(package_dir)])
            archives = list(package_dir.glob("*.tgz"))
            if len(archives) != 1:
                raise ChartError(f"expected one package for chart {chart.name}")
            identity = (
                chart.repository,
                chart.source_name or chart.name,
                chart.target_version,
                chart.sha256,
                chart.remove_templates,
                chart.replace_templates,
            )
            if identity not in pushed:
                _run(["helm", "push", str(archives[0]), manifest.destination])
                pushed.add(identity)
            if chart.vendor_path:
                _replace_vendor(chart_dir, repository_root, chart.vendor_path)
            mirrored.append({
                "name": chart.name,
                "version": chart.target_version,
                "resources": len(identities),
                "package_sha256": _sha256(archives[0]),
            })
    return {"destination": manifest.destination, "charts": mirrored}


def format_result(result: dict[str, Any]) -> str:
    return json.dumps(result, sort_keys=True)
