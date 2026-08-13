from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


def render_aggregate_release(
    aggregate_tag: str,
    shared_summary: str,
    service_entries: list[tuple[str, str]],
    deployed_services: list[str],
) -> str:
    sections = [f"# {aggregate_tag}", ""]
    shared = shared_summary.strip()
    if shared:
        sections.extend(["## Shared changes", "", shared, ""])
    if service_entries:
        sections.extend(["## Service-specific changes", ""])
        for service, summary in service_entries:
            sections.extend([f"### {service}", "", summary.strip(), ""])
    sections.extend(["## Deployed services", ""])
    sections.extend(f"- {service}" for service in deployed_services)
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
        request = Request(endpoint, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=120) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"DeepSeek release summary failed: {exc}") from exc
        try:
            choice = result["choices"][0]
            finish_reason = str(choice.get("finish_reason", "unknown"))
            summary = str(choice["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ReleaseError("DeepSeek returned an invalid response") from exc
        if summary:
            return summary[:8000]
    raise ReleaseError(f"DeepSeek returned an empty response after retry (finish_reason={finish_reason})")
