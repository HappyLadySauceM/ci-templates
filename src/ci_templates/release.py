from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


def render_aggregate_release(
    project: str,
    aggregate_tag: str,
    entries: list[tuple[str, str, str]],
) -> str:
    sections = [f"# {project} {aggregate_tag}", "", "## Functional changes", ""]
    for service, tag, summary in entries:
        sections.extend([f"### {service} · `{tag}`", "", summary.strip(), ""])
    sections.extend(["## Service versions", ""])
    sections.extend(f"- {service}: `{tag}`" for service, tag, _ in entries)
    return "\n".join(sections).rstrip() + "\n"


def summarize_with_deepseek(model: str, service: str, version: str, changes: dict, language: str = "en") -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    if not api_key:
        raise ReleaseError("DEEPSEEK_API_KEY is required before main promotion")
    prompt = {
        "service": service,
        "version": version,
        "changes": changes,
        "instruction": (
            f"Write a concise functional change summary in {language}. Use only the supplied changed paths and diff. "
            "Describe user-visible behavior, API, configuration, or operational changes when evidenced. "
            "Do not mention commit hashes, branches, workflows, authors, or release metadata. Never invent details. "
            "Return Markdown bullets only."
        ),
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=True)}],
        "temperature": 0.1,
        "max_tokens": 900,
    }
    request = Request(endpoint, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"DeepSeek release summary failed: {exc}") from exc
    try:
        summary = str(result["choices"][0]["message"]["content"]).strip()
        if not summary:
            raise ReleaseError("DeepSeek returned an empty response")
        return summary[:8000]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseError("DeepSeek returned an invalid response") from exc
