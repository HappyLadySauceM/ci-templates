from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from pathlib import Path


class HarborError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str

    @classmethod
    def parse(cls, value: str) -> "ImageRef":
        without_digest = value.split("@", 1)[0]
        if "://" in without_digest or "/" not in without_digest:
            raise ValueError(f"image must include registry and tag: {value!r}")
        name, tag = without_digest.rsplit(":", 1)
        registry, repository = name.split("/", 1)
        if not registry or not repository or not tag:
            raise ValueError(f"invalid image reference: {value!r}")
        return cls(registry, repository, tag)

    @property
    def tag_ref(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag}"


class HarborClient:
    def __init__(self, registry: str, username: str | None = None, password: str | None = None, timeout: float = 20.0):
        self.registry = registry.rstrip("/")
        self.username = username or os.environ.get("HARBOR_USERNAME", "")
        self.password = password or os.environ.get("HARBOR_PASSWORD", "")
        if not self.username:
            self.username, self.password = self._docker_credentials()
        self.timeout = timeout

    def _docker_credentials(self) -> tuple[str, str]:
        config_root = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        config_path = config_root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "", ""
        auths = config.get("auths", {})
        for key in (self.registry, f"https://{self.registry}"):
            encoded = auths.get(key, {}).get("auth")
            if not encoded:
                continue
            try:
                value = base64.b64decode(encoded).decode()
                return tuple(value.split(":", 1)) if ":" in value else ("", "")
            except (ValueError, UnicodeDecodeError):
                return "", ""
        return "", ""

    def _request(self, method: str, path: str, *, body: object | None = None, accept: str = "application/json"):
        headers = {"Accept": accept}
        if self.username:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        request = Request(f"https://{self.registry}{path}", data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, response.headers, response.read()
        except (HTTPError, URLError) as exc:
            raise HarborError(f"Harbor {method} {path} failed: {exc}") from exc

    def manifest_digest(self, image: ImageRef) -> str | None:
        path = f"/v2/{image.repository}/manifests/{quote(image.tag, safe='') }"
        try:
            _, headers, _ = self._request("HEAD", path, accept="application/vnd.oci.image.manifest.v1+json")
        except HarborError as exc:
            if "HTTP Error 404" in str(exc):
                return None
            raise
        return headers.get("Docker-Content-Digest")

    def delete_tag(self, image: ImageRef) -> None:
        parts = image.repository.split("/", 1)
        if len(parts) != 2:
            raise HarborError("repository must be project/name for tag deletion")
        project, repository = parts
        digest = self.manifest_digest(image)
        if not digest:
            return
        repo_path = quote(repository, safe="")
        self._request("DELETE", f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{repo_path}/artifacts/{quote(digest, safe='')}/tags/{quote(image.tag, safe='')}")

    def promote_tag(self, source: ImageRef, destination: ImageRef) -> str:
        """Move a candidate tag to the active tag without pulling the image.

        Harbor stores tags as references to immutable manifests.  Promotion is
        therefore a small API operation and does not require a Docker daemon
        (or a privileged runner) to be available.
        """
        if source.registry != destination.registry or source.repository != destination.repository:
            raise HarborError("source and destination must reference the same image repository")
        digest = self.manifest_digest(source)
        if not digest:
            raise HarborError(f"source image does not exist: {source.tag_ref}")
        existing = self.manifest_digest(destination)
        if existing == digest:
            return digest
        if existing:
            self.delete_tag(destination)
        parts = destination.repository.split("/", 1)
        if len(parts) != 2:
            raise HarborError("repository must be project/name for tag promotion")
        project, repository = parts
        repo_path = quote(repository, safe="")
        try:
            self._request(
                "POST",
                f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{repo_path}/artifacts/{quote(digest, safe='')}/tags",
                body={"name": destination.tag},
            )
            promoted = self.manifest_digest(destination)
            if promoted != digest:
                raise HarborError(f"Harbor promotion verification failed for {destination.tag_ref}")
        except HarborError:
            # Deleting an existing active tag is the one non-idempotent step.
            # If the new tag cannot be attached, restore the old reference so
            # a transient Harbor/API failure cannot leave the service without
            # its last known-good active image.
            if existing:
                try:
                    self._request(
                        "POST",
                        f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{repo_path}/artifacts/{quote(existing, safe='')}/tags",
                        body={"name": destination.tag},
                    )
                except HarborError as restore_exc:
                    raise HarborError(
                        f"Harbor promotion failed and restoring {destination.tag_ref} failed"
                    ) from restore_exc
            raise
        return digest
