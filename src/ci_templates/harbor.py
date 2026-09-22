from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any, Collection

from .transport import request_with_retry


class HarborError(RuntimeError):
    pass


class HarborHTTPError(HarborError):
    def __init__(self, method: str, path: str, status_code: int):
        self.status_code = status_code
        super().__init__(f"Harbor {method} {path} failed: HTTP {status_code}")


class HarborTransportError(HarborError):
    pass


class HarborDigestConflict(HarborError):
    def __init__(self, image: "ImageRef", expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"digest-conflict for {image.tag_ref}: expected {expected}, found {actual}"
        )


# Active tags such as :dev are often a manifest list/index, not a single image
# manifest. Asking only for OCI image manifests makes registry HEAD return 404,
# so promotion skips delete and POST /tags conflicts (409).
# :dev 这类活跃 tag 经常是 manifest list/index 而不是单层 image manifest。
# 只请求 OCI image manifest 时 registry HEAD 会 404，promote 跳过删除后
# POST /tags 就会 409。
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


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

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: object | None = None,
        accept: str = "application/json",
        accepted_statuses: Collection[int] = (),
    ):
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
            response_context = request_with_retry(
                urlopen,
                request,
                timeout=self.timeout,
                allow_write_retry=False,
            )
            with response_context as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            try:
                if exc.code in accepted_statuses:
                    return exc.code, exc.headers, exc.read()
            finally:
                exc.close()
            raise HarborHTTPError(method, path, exc.code) from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            raise HarborTransportError(f"Harbor {method} {path} failed: {exc}") from exc

    def manifest_digest(self, image: ImageRef) -> str | None:
        path = f"/v2/{image.repository}/manifests/{quote(image.tag, safe='') }"
        status, headers, _ = self._request(
            "HEAD", path, accept=_MANIFEST_ACCEPT, accepted_statuses={404}
        )
        if status == 404:
            return None
        digest = headers.get("Docker-Content-Digest")
        if not digest:
            raise HarborError(f"Harbor manifest response omitted digest for {image.tag_ref}")
        return digest

    def delete_tag(self, image: ImageRef, *, expected_digest: str | None = None) -> str:
        parts = image.repository.split("/", 1)
        if len(parts) != 2:
            raise HarborError("repository must be project/name for tag deletion")
        project, repository = parts
        digest = self.manifest_digest(image)
        if not digest:
            return "already-absent"
        if expected_digest is not None and digest != expected_digest:
            return "changed"
        repo_path = quote(repository, safe="")
        path = f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{repo_path}/artifacts/{quote(digest, safe='')}/tags/{quote(image.tag, safe='')}"
        try:
            status, _, _ = self._request("DELETE", path, accepted_statuses={404})
        except (HarborTransportError, HarborHTTPError) as exc:
            if isinstance(exc, HarborHTTPError) and exc.status_code < 500:
                raise
            # DELETE may have succeeded even if the response was lost. Confirm
            # the post-condition before reporting an uncertain write failure.
            try:
                current = self.manifest_digest(image)
            except HarborError as read_exc:
                raise exc from read_exc
            if current is None:
                return "reconciled-after-delete"
            if current != digest:
                return "changed"
            raise
        if status == 404:
            current = self.manifest_digest(image)
            if current is None:
                return "already-absent"
            if current != digest:
                return "changed"
            raise HarborHTTPError("DELETE", path, 404)
        return "deleted"

    def list_candidate_tags(self, project: str, *, prefix: str = "sha-", page_size: int = 100) -> list[dict[str, Any]]:
        """List candidate tags with their push time and manifest digest."""

        results: list[dict[str, Any]] = []
        page = 1
        while True:
            status, _, payload = self._request(
                "GET",
                f"/api/v2.0/projects/{quote(project, safe='')}/repositories?page={page}&page_size={page_size}",
            )
            repositories = json.loads(payload or b"[]")
            if not isinstance(repositories, list) or not repositories:
                break
            for repository in repositories:
                repository_name = str(repository.get("name") or "")
                if "/" in repository_name:
                    repository_name = repository_name.split("/", 1)[1]
                if not repository_name:
                    continue
                artifact_page = 1
                while True:
                    _, _, artifact_payload = self._request(
                        "GET",
                        f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{quote(repository_name, safe='')}/artifacts?with_tag=true&page={artifact_page}&page_size={page_size}",
                    )
                    artifacts = json.loads(artifact_payload or b"[]")
                    if not isinstance(artifacts, list) or not artifacts:
                        break
                    for artifact in artifacts:
                        digest = str(artifact.get("digest") or "")
                        for tag in artifact.get("tags") or []:
                            name = str(tag.get("name") or "")
                            if name.startswith(prefix):
                                results.append({
                                    "repository": repository_name,
                                    "tag": name,
                                    "digest": digest,
                                    "push_time": str(tag.get("push_time") or artifact.get("push_time") or ""),
                                })
                    if len(artifacts) < page_size:
                        break
                    artifact_page += 1
            if len(repositories) < page_size:
                break
            page += 1
        return results

    def tag_digest(self, image: ImageRef, digest: str) -> str:
        """Attach a tag only when absent or already pointing at ``digest``."""

        project, repository = image.repository.split("/", 1)
        repo_path = quote(repository, safe="")
        current = self.manifest_digest(image)
        if current == digest:
            return "already-present"
        if current is not None:
            raise HarborDigestConflict(image, digest, current)

        path = f"/api/v2.0/projects/{quote(project, safe='')}/repositories/{repo_path}/artifacts/{quote(digest, safe='')}/tags"
        try:
            status, _, _ = self._request(
                "POST", path, body={"name": image.tag}, accepted_statuses={409}
            )
        except HarborTransportError as exc:
            return self._reconcile_tag_write(image, digest, exc)
        except HarborHTTPError as exc:
            if exc.status_code in {408, 425, 429} or exc.status_code >= 500:
                return self._reconcile_tag_write(image, digest, exc)
            raise

        if status == 409:
            return self._reconcile_tag_write(
                image, digest, HarborHTTPError("POST", path, 409)
            )
        actual = self.manifest_digest(image)
        if actual == digest:
            return "created"
        if actual is not None:
            raise HarborDigestConflict(image, digest, actual)
        raise HarborError(f"candidate tag creation was not visible for {image.tag_ref}")

    def _reconcile_tag_write(self, image: ImageRef, digest: str, error: HarborError) -> str:
        try:
            actual = self.manifest_digest(image)
        except HarborError as read_exc:
            raise error from read_exc
        if actual == digest:
            return "reconciled-after-conflict"
        if actual is not None:
            raise HarborDigestConflict(image, digest, actual) from error
        raise error

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
            self.delete_tag(destination, expected_digest=existing)
            after_delete = self.manifest_digest(destination)
            if after_delete == digest:
                return digest
            if after_delete is not None:
                raise HarborDigestConflict(destination, digest, after_delete)
        try:
            self.tag_digest(destination, digest)
        except HarborError as promotion_error:
            # Reconcile first: another runner may have completed this
            # promotion while this request was in flight.
            try:
                current = self.manifest_digest(destination)
            except HarborError as read_exc:
                raise promotion_error from read_exc
            if current == digest:
                return digest
            if current is not None:
                raise HarborDigestConflict(destination, digest, current) from promotion_error
            # Restore the previous active reference only if the destination is
            # still absent. Never overwrite a tag another runner has created.
            if existing:
                try:
                    self.tag_digest(destination, existing)
                except HarborError as restore_exc:
                    raise HarborError(
                        f"Harbor promotion failed and restoring {destination.tag_ref} failed"
                    ) from restore_exc
            raise
        return digest
