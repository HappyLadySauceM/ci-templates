from __future__ import annotations

import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_DEEPSEEK_ATTEMPTS = 3
_VERSION_HEADING = re.compile(r"^#{1,6}\s*v?\d+\.\d+(?:\.\d+)?\b.*$", re.IGNORECASE)


def strip_release_version_heading(text: str) -> str:
    """Drop leading Markdown version headings such as # v0.1.1.

    去掉正文开头的版本标题（例如 # v0.1.1），GitHub Release 名已经有 tag。
    """

    lines = text.splitlines()
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        if _VERSION_HEADING.match(first):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _deepseek_request(endpoint: str, api_key: str, body: dict, *, validate=None) -> dict:
    """Make one bounded remote request, retrying only transient failures."""
    request = Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(_DEEPSEEK_ATTEMPTS):
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(response.read())
                if validate is not None and not validate(result):
                    raise ValueError("DeepSeek returned an empty or invalid response")
                return result
        except HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_STATUS:
                raise ReleaseError(f"DeepSeek release summary failed: HTTP {exc.code}") from exc
            last_error = exc
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < _DEEPSEEK_ATTEMPTS - 1:
            time.sleep(2**attempt)
    raise ReleaseError(f"DeepSeek release summary failed after {_DEEPSEEK_ATTEMPTS} attempts: {last_error}") from last_error


def render_aggregate_release(
    aggregate_tag: str,
    shared_summary: str,
    service_entries: list[tuple[str, str]],
    deployed_services: list[str],
) -> str:
    del aggregate_tag
    sections: list[str] = []
    shared = shared_summary.strip()
    if shared:
        sections.extend(["## Shared changes", "", shared, ""])
    if service_entries:
        sections.extend(["## Service-specific changes", ""])
        for service, summary in service_entries:
            sections.extend([f"### {service}", "", summary.strip(), ""])
    if deployed_services:
        sections.extend(["## Affected services", ""])
        sections.extend(f"- {service}" for service in deployed_services)
    else:
        sections.extend(["## Deployment scope", "", "- Shared deployment configuration"])
    return "\n".join(sections).rstrip() + "\n"


def summarize_with_deepseek(
    model: str,
    service: str,
    version: str,
    changes: dict,
    language: str = "en",
    *,
    shared: bool = False,
) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    if not api_key:
        raise ReleaseError("DEEPSEEK_API_KEY is required before main promotion")
    if shared:
        instruction = (
            f"Write a concise functional change summary in {language} for cross-service shared, "
            "CI/CD, build, or platform changes. Use only the supplied changed paths and diff. "
            "Describe the change once for the whole project; do not attribute the same change to "
            "individual services. Describe user-visible behavior, API, configuration, or operational "
            "changes when evidenced. Do not mention commit hashes, branches, workflows, authors, or "
            "release metadata. Never invent details. Return Markdown bullets only."
        )
    else:
        instruction = (
            f"Write a concise functional change summary in {language} for this service only. "
            "Use only the supplied changed paths and diff. Describe user-visible behavior, API, "
            "configuration, or operational changes when evidenced. Do not mention commit hashes, "
            "branches, workflows, authors, or release metadata. Never invent details. "
            "Return Markdown bullets only."
        )
    prompt = {
        "service": service,
        "version": version,
        "changes": changes,
        "scope": "shared" if shared else "service",
        "instruction": instruction,
    }
    finish_reason = "unknown"
    for max_tokens in (4096, 8192):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=True)}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        result = _deepseek_request(endpoint, api_key, body)
        try:
            choice = result["choices"][0]
            finish_reason = str(choice.get("finish_reason", "unknown"))
            summary = str(choice["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ReleaseError("DeepSeek returned an invalid response") from exc
        if summary:
            return summary[:8000]
    raise ReleaseError(f"DeepSeek returned an empty response after retry (finish_reason={finish_reason})")


def summarize_release_with_deepseek(
    model: str,
    aggregate_tag: str,
    context: dict,
    deployed_services: list[str],
    language: str = "en",
) -> str:
    """Create one bounded project summary for all shared and service changes."""
    del aggregate_tag
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    if not api_key:
        raise ReleaseError("DEEPSEEK_API_KEY is required before deployment")
    prompt = {
        "changes": context,
        "deployed_services": deployed_services,
        "instruction": (
            f"Write a concise functional release summary in {language}. Use only the supplied "
            "redacted paths and diffs. Cover shared changes and service-specific changes in "
            "separate Markdown bullet groups, omit empty groups, and never mention commit hashes, "
            "branches, workflows, authors, credentials, version numbers, tags, VERSION files, "
            "or release metadata. Never invent details. Return Markdown bullets only, without "
            "a title. Do not start with a heading."
        ),
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=True)}],
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    result = _deepseek_request(
        endpoint,
        api_key,
        body,
        validate=lambda value: bool(value.get("choices", [{}])[0].get("message", {}).get("content"))
        if isinstance(value, dict) and isinstance(value.get("choices"), list) and value.get("choices")
        else False,
    )
    try:
        summary = str(result["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseError("DeepSeek returned an invalid response") from exc
    if not summary:
        raise ReleaseError("DeepSeek returned an empty response")
    summary = strip_release_version_heading(summary[:12000])
    if not summary:
        raise ReleaseError("DeepSeek returned an empty response")
    sections = [summary, ""]
    if deployed_services:
        sections.extend(["## Affected services", ""])
        sections.extend(f"- {service}" for service in deployed_services)
    else:
        sections.extend(["## Deployment scope", "", "- Shared deployment configuration"])
    return "\n".join(sections).rstrip() + "\n"
