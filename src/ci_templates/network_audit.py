from __future__ import annotations
import re
from pathlib import Path

_FORBIDDEN = (
    (re.compile(r"uses:\s*actions/upload-artifact@"), "use upload-artifact-with-retry"),
    (re.compile(r"(^|\s)(curl|wget)\s"), "register the HTTP call in ci-templates"),
    (re.compile(r"(^|\s)git fetch\s"), "wrap git fetch with ci-templates network-run"),
)

def audit_workflows(root: str) -> list[str]:
    failures: list[str] = []
    base = Path(root)
    for path in sorted((base / ".github" / "workflows").glob("*.y*ml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "ci-templates network-run" in line:
                continue
            for pattern, remedy in _FORBIDDEN:
                if pattern.search(line):
                    failures.append(f"{path}:{number}: {remedy}")
    return failures
