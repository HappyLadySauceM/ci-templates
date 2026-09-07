from __future__ import annotations

import json
import os
import time
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Service


class ArgoError(RuntimeError):
    pass


POLL_SECONDS = 5
REFRESH_INTERVAL_SECONDS = 30
POD_FAILURE_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "InvalidImageName",
    }
)
WORKLOAD_KINDS = frozenset({"StatefulSet", "Deployment", "DaemonSet"})
TERMINAL_OPERATION_PHASES = frozenset({"Failed", "Error", "Terminated"})


def _has_revision(sync: dict, revision: str) -> bool:
    if sync.get("revision") == revision:
        return True
    revisions = sync.get("revisions", [])
    return isinstance(revisions, list) and revision in revisions


def _observed_revisions(payload: dict) -> set[str]:
    status = payload.get("status") or {}
    revisions: set[str] = set()
    sync = status.get("sync") or {}
    current = sync.get("revision")
    if isinstance(current, str) and current:
        revisions.add(current)
    extra = sync.get("revisions")
    if isinstance(extra, list):
        revisions.update(item for item in extra if isinstance(item, str) and item)
    operation = status.get("operationState") or {}
    for container in (operation, operation.get("syncResult") or {}, operation.get("operation") or {}):
        if not isinstance(container, dict):
            continue
        value = container.get("revision")
        if isinstance(value, str) and value:
            revisions.add(value)
        nested = container.get("sync")
        if isinstance(nested, dict):
            nested_revision = nested.get("revision")
            if isinstance(nested_revision, str) and nested_revision:
                revisions.add(nested_revision)
    history = status.get("history") or []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            value = entry.get("revision")
            if isinstance(value, str) and value:
                revisions.add(value)
    return revisions


def _summary_images(payload: dict) -> set[str]:
    status = payload.get("status") or {}
    summary = status.get("summary") or {}
    images = summary.get("images") or []
    if not isinstance(images, list):
        return set()
    return {item for item in images if isinstance(item, str) and item}


def _resource_label(resource: dict) -> str:
    group = resource.get("group")
    kind = resource.get("kind") or "Resource"
    name = resource.get("name") or "unknown"
    return f"{group + '/' if group else ''}{kind}/{name}"


def _state_description(payload: dict, expected_images: tuple[str, ...] = ()) -> str:
    status = payload.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    operation = status.get("operationState") or {}
    observed_images = _summary_images(payload)
    state = (
        f"revision={sync.get('revision')} sync={sync.get('status')} "
        f"health={health.get('status')} operation={operation.get('phase', '-')}"
    )
    message = operation.get("message") or health.get("message")
    if message:
        state += f" message={str(message).replace(chr(10), ' ')}"
    if expected_images:
        state += f" images={','.join(sorted(observed_images)) or 'none'}"
    resources = status.get("resources") or []
    if isinstance(resources, list) and resources:
        details: list[str] = []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            health_info = resource.get("health") or {}
            detail = f"{_resource_label(resource)}={resource.get('status', '-')}"
            if health_info.get("status"):
                detail += f"/{health_info['status']}"
            if resource.get("requiresPruning"):
                detail += "/requiresPruning"
            details.append(detail)
        if details:
            state += f" resources={';'.join(details)}"
    return state


def _payload_failure(payload: dict) -> str | None:
    status = payload.get("status") or {}
    operation = status.get("operationState") or {}
    phase = operation.get("phase")
    if phase in TERMINAL_OPERATION_PHASES:
        return f"operation phase is {phase}"
    health = status.get("health") or {}
    if health.get("status") == "Degraded":
        return f"application health is Degraded: {health.get('message') or 'no message'}"
    resources = status.get("resources") or []
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            resource_health = resource.get("health") or {}
            if resource_health.get("status") == "Degraded":
                return (
                    f"resource {_resource_label(resource)} is Degraded: "
                    f"{resource_health.get('message') or 'no message'}"
                )
    return None


def _kubectl_json(kubeconfig: str, arguments: list[str]) -> dict | None:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, *arguments, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tracked_workloads(payload: dict) -> list[tuple[str, str, str]]:
    status = payload.get("status") or {}
    resources = status.get("resources") or []
    destination = (payload.get("spec") or {}).get("destination") or {}
    default_namespace = destination.get("namespace") or "default"
    workloads: list[tuple[str, str, str]] = []
    if not isinstance(resources, list):
        return workloads
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("kind") not in WORKLOAD_KINDS:
            continue
        name = resource.get("name")
        if not isinstance(name, str) or not name:
            continue
        namespace = resource.get("namespace") or default_namespace
        workloads.append((resource["kind"], namespace, name))
    return workloads


def _pod_failures(kubeconfig: str, payload: dict) -> list[dict[str, str | int]]:
    failures: list[dict[str, str | int]] = []
    for kind, namespace, name in _tracked_workloads(payload):
        resource_name = kind.lower()
        workload = _kubectl_json(
            kubeconfig,
            ["get", resource_name, name, "-n", namespace],
        )
        if not workload:
            continue
        labels = ((workload.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not isinstance(labels, dict) or not labels:
            continue
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        pods = _kubectl_json(
            kubeconfig,
            ["get", "pods", "-n", namespace, "-l", selector],
        )
        for pod in (pods or {}).get("items", []):
            if not isinstance(pod, dict):
                continue
            pod_name = (pod.get("metadata") or {}).get("name")
            if not isinstance(pod_name, str) or not pod_name:
                continue
            statuses = []
            pod_status = pod.get("status") or {}
            statuses.extend(pod_status.get("initContainerStatuses") or [])
            statuses.extend(pod_status.get("containerStatuses") or [])
            for container_status in statuses:
                if not isinstance(container_status, dict):
                    continue
                current = container_status.get("state") or {}
                waiting = current.get("waiting") or {}
                terminated = current.get("terminated") or {}
                reason = waiting.get("reason") or terminated.get("reason")
                exit_code = terminated.get("exitCode")
                if reason not in POD_FAILURE_REASONS and not (
                    isinstance(exit_code, int) and exit_code != 0
                ):
                    continue
                failures.append(
                    {
                        "workload": f"{kind}/{name}",
                        "namespace": namespace,
                        "pod": pod_name,
                        "container": str(container_status.get("name") or "unknown"),
                        "reason": str(reason or f"exitCode={exit_code}"),
                        "restarts": int(container_status.get("restartCount") or 0),
                        "message": str(waiting.get("message") or terminated.get("message") or ""),
                    }
                )
    return failures


def _pod_logs(kubeconfig: str, failure: dict[str, str | int]) -> str:
    base = [
        "kubectl",
        "--kubeconfig",
        kubeconfig,
        "logs",
        str(failure["pod"]),
        "-n",
        str(failure["namespace"]),
        "-c",
        str(failure["container"]),
        "--tail=80",
    ]
    for previous in (True, False):
        command = base + (["--previous"] if previous else [])
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        output = (result.stdout or result.stderr).strip()
        if output:
            if len(output) > 8_000:
                output = output[-8_000:]
            label = "previous" if previous else "current"
            return f"logs({label})={output}"
    return "logs=unavailable"


def _format_failure(
    application: str,
    payload: dict,
    reason: str,
    kubeconfig: str = "",
    pod_failures: list[dict[str, str | int]] | None = None,
    expected_images: tuple[str, ...] = (),
) -> str:
    lines = [
        f"Argo application {application} failed fast: {reason}",
        f"  {_state_description(payload, expected_images)}",
    ]
    for failure in pod_failures or []:
        lines.append(
            "  pod="
            f"{failure['namespace']}/{failure['pod']} container={failure['container']} "
            f"workload={failure['workload']} reason={failure['reason']} "
            f"restarts={failure['restarts']} message={failure['message']}"
        )
        if kubeconfig:
            lines.append(f"  {_pod_logs(kubeconfig, failure)}")
    return "\n".join(lines)


def _ready_state(payload: dict, revision: str, expected_images: tuple[str, ...] = ()) -> tuple[bool, str]:
    status = payload.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    observed_images = _summary_images(payload)
    state = _state_description(payload, expected_images)
    synced_healthy = sync.get("status") == "Synced" and health.get("status") == "Healthy"
    revision_ready = revision in _observed_revisions(payload)
    # Empty expected_images must not pass a Healthy app at another Git SHA.
    # 未提供期望镜像时，不能把「别的 SHA 上 Healthy」当成成功。
    images_ready = bool(expected_images) and all(image in observed_images for image in expected_images)
    ready = synced_healthy and (revision_ready or images_ready)
    return ready, state


def wait_targets(
    services: tuple[Service, ...] | list[Service],
    overrides: dict[str, dict[str, str]] | None = None,
    application_suffix: str = "-dev",
) -> dict[str, tuple[str, ...]]:
    """Map pipeline services and image overrides to Argo applications.

    将流水线服务与镜像覆盖映射为 Argo Application 及期望镜像。
    """
    images_by_app: dict[str, tuple[str, ...]] = {}
    for service in services:
        application = f"{service.kustomize_name}{application_suffix}"
        override = (overrides or {}).get(service.kustomize_name) or {}
        repository = override.get("newName") or service.image_repository
        digest = override.get("digest", "")
        tag = override.get("newTag", "")
        if digest:
            images_by_app[application] = (f"{repository}@{digest}",)
        elif tag:
            images_by_app[application] = (f"{repository}:{tag}",)
        else:
            images_by_app[application] = ()
    return images_by_app


def _request_hard_refresh(kubeconfig: str, application: str, namespace: str = "argocd") -> None:
    # Ask the application controller to compare against current Git HEAD even when live
    # resources already match after ignoreDifferences.
    # 强制用当前 Git HEAD 做对比；ignoreDifferences 导致无 diff 时也能刷新 revision。
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "-n",
            namespace,
            "annotate",
            "application",
            application,
            "argocd.argoproj.io/refresh=hard",
            "--overwrite",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise ArgoError(f"failed to hard-refresh Argo application {application}: {detail}")


def _get_application(kubeconfig: str, application: str, namespace: str = "argocd") -> tuple[dict | None, str]:
    result = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "get",
            "application",
            application,
            "-n",
            namespace,
            "-o",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or f"exit {result.returncode}"
    try:
        return json.loads(result.stdout), "unknown"
    except json.JSONDecodeError as exc:
        raise ArgoError(f"kubectl returned invalid Argo application JSON for {application}") from exc


def terminate_operations(
    kubeconfig: str,
    applications: tuple[str, ...] | list[str],
    timeout: int = 60,
    argocd_namespace: str = "argocd",
) -> None:
    names = tuple(application for application in applications if application)
    if not names:
        raise ArgoError("at least one Argo application is required")
    if not kubeconfig:
        raise ArgoError("KUBECONFIG is required to terminate Argo operations")
    deadline = time.monotonic() + timeout
    for application in names:
        payload, detail = _get_application(kubeconfig, application, argocd_namespace)
        if payload is None:
            raise ArgoError(f"cannot inspect Argo application {application}: {detail}")
        if payload.get("operation") is None:
            continue
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                kubeconfig,
                "-n",
                argocd_namespace,
                "patch",
                "application",
                application,
                "--type=merge",
                "-p",
                '{"operation":null}',
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise ArgoError(f"failed to terminate Argo operation {application}: {detail}")

    pending = set(names)
    while pending and time.monotonic() < deadline:
        for application in tuple(pending):
            payload, detail = _get_application(kubeconfig, application, argocd_namespace)
            if payload is None:
                raise ArgoError(f"cannot inspect Argo application {application}: {detail}")
            if payload.get("operation") is None:
                pending.discard(application)
        if pending:
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    if pending:
        raise ArgoError(
            "Argo operations did not terminate: " + ", ".join(sorted(pending))
        )


def wait_applications(
    server: str,
    applications: tuple[str, ...] | list[str],
    revision: str,
    timeout: int = 600,
    refresh_interval: int = REFRESH_INTERVAL_SECONDS,
    expected_images: dict[str, tuple[str, ...]] | None = None,
    argocd_namespace: str = "argocd",
) -> dict[str, dict]:
    names = tuple(application for application in applications if application)
    if not names:
        raise ArgoError("at least one Argo application is required")
    images_by_app = expected_images or {}
    kubeconfig = os.environ.get("KUBECONFIG", "")
    pending = set(names)
    payloads: dict[str, dict] = {}
    last_states = {name: "unknown" for name in names}
    last_payloads: dict[str, dict] = {}
    if kubeconfig:
        deadline = time.monotonic() + timeout
        last_refresh = 0.0
        refresh_observed = {name: False for name in names}
        pod_failure_counts = {name: 0 for name in names}
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_refresh >= max(0, refresh_interval):
                for application in names:
                    if application in pending and not refresh_observed[application]:
                        _request_hard_refresh(kubeconfig, application, argocd_namespace)
                last_refresh = now
            for application in names:
                if application not in pending:
                    continue
                payload, last_state = _get_application(kubeconfig, application, argocd_namespace)
                if payload is None:
                    last_states[application] = last_state
                    continue
                last_payloads[application] = payload
                if revision in _observed_revisions(payload) or (
                    images_by_app.get(application)
                    and all(image in _summary_images(payload) for image in images_by_app[application])
                ):
                    refresh_observed[application] = True
                failure = _payload_failure(payload)
                if failure:
                    raise ArgoError(
                        _format_failure(
                            application,
                            payload,
                            failure,
                            kubeconfig,
                            expected_images=images_by_app.get(application, ()),
                        )
                    )
                pod_failures = _pod_failures(kubeconfig, payload)
                if pod_failures:
                    pod_failure_counts[application] += 1
                    if pod_failure_counts[application] >= 2:
                        raise ArgoError(
                            _format_failure(
                                application,
                                payload,
                                "tracked workload has a persistent failing Pod",
                                kubeconfig,
                                pod_failures,
                                images_by_app.get(application, ()),
                            )
                        )
                else:
                    pod_failure_counts[application] = 0
                ready, last_states[application] = _ready_state(
                    payload,
                    revision,
                    expected_images=images_by_app.get(application, ()),
                )
                if ready:
                    payloads[application] = payload
                    pending.discard(application)
            if not pending:
                return payloads
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        remaining_parts = []
        for name in names:
            if name not in pending:
                continue
            payload = last_payloads.get(name)
            if payload:
                failures = _pod_failures(kubeconfig, payload)
                if failures:
                    remaining_parts.append(
                        _format_failure(
                            name,
                            payload,
                            "timeout while tracked workload remains unhealthy",
                            kubeconfig,
                            failures,
                            images_by_app.get(name, ()),
                        )
                    )
                    continue
            remaining_parts.append(f"{name}: {last_states[name]}")
        remaining = "\n".join(remaining_parts)
        raise ArgoError(f"Argo application did not become healthy at revision {revision}: {remaining}")
    token = os.environ.get("ARGOCD_AUTH_TOKEN", "")
    endpoint = os.environ.get("ARGOCD_SERVER", server).rstrip("/")
    deadline = time.monotonic() + timeout
    last_refresh = 0.0
    refresh_observed = {name: False for name in names}
    while time.monotonic() < deadline:
        now = time.monotonic()
        refresh_due = now - last_refresh >= max(0, refresh_interval)
        if refresh_due:
            last_refresh = now
        for application in names:
            if application not in pending:
                continue
            url = f"https://{endpoint}/api/v1/applications/{application}"
            if refresh_due and not refresh_observed[application]:
                url += "?refresh=hard"
            request = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urlopen(request, timeout=min(20, max(1, int(deadline - time.monotonic())))) as response:
                    payload = json.loads(response.read())
            except (HTTPError, URLError, json.JSONDecodeError) as exc:
                last_states[application] = str(exc)
                continue
            last_payloads[application] = payload
            if revision in _observed_revisions(payload) or (
                images_by_app.get(application)
                and all(image in _summary_images(payload) for image in images_by_app[application])
            ):
                refresh_observed[application] = True
            failure = _payload_failure(payload)
            if failure:
                raise ArgoError(
                    _format_failure(
                        application,
                        payload,
                        failure,
                        expected_images=images_by_app.get(application, ()),
                    )
                )
            ready, last_states[application] = _ready_state(
                payload,
                revision,
                expected_images=images_by_app.get(application, ()),
            )
            if ready:
                payloads[application] = payload
                pending.discard(application)
        if not pending:
            return payloads
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
    remaining = "\n".join(
        f"{name}: {last_states[name]}" for name in names if name in pending
    )
    raise ArgoError(f"Argo application did not become healthy at revision {revision}: {remaining}")


def wait_application(
    server: str,
    application: str,
    revision: str,
    timeout: int = 600,
    refresh_interval: int = REFRESH_INTERVAL_SECONDS,
    expected_images: tuple[str, ...] = (),
    argocd_namespace: str = "argocd",
) -> dict:
    payloads = wait_applications(
        server,
        (application,),
        revision,
        timeout=timeout,
        refresh_interval=refresh_interval,
        expected_images={application: expected_images} if expected_images else None,
        argocd_namespace=argocd_namespace,
    )
    return payloads[application]
