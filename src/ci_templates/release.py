from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ReleaseError(RuntimeError):
    pass


def summarize_with_deepseek(model: str, service: str, version: str, metadata: dict[str, str]) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    endpoint = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
    if not api_key:
        raise ReleaseError("DEEPSEEK_API_KEY is required before main promotion")
    prompt = {
        "service": service,
        "version": version,
        "metadata": metadata,
        "instruction": "Write a concise release summary from the supplied metadata only; never invent details.",
    }
    body = {"model": model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=True)}], "temperature": 0.1}
    request = Request(endpoint, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"DeepSeek release summary failed: {exc}") from exc
    try:
        return str(result["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ReleaseError("DeepSeek returned an invalid response") from exc
