"""Bounded, observable network retries shared by CI workflows and clients."""
from __future__ import annotations
import email.utils, json, os, random, subprocess, sys, time
from dataclasses import dataclass
from datetime import timezone
from enum import Enum
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

class NetworkOperation(str, Enum):
    READ = "read"
    DOWNLOAD = "download"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"

@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    initial_delay: float = 1.0
    maximum_delay: float = 30.0
    jitter: float = 0.25
    retryable_statuses: frozenset[int] = RETRYABLE_STATUS_CODES
    def __post_init__(self) -> None:
        if self.attempts < 1 or min(self.initial_delay, self.maximum_delay, self.jitter) < 0:
            raise ValueError("invalid retry policy")

READ_RETRY_POLICY = RetryPolicy()
WRITE_RETRY_POLICY = RetryPolicy(attempts=3, initial_delay=2, maximum_delay=15)
NO_RETRY_POLICY = RetryPolicy(attempts=1)

def policy_for(operation: NetworkOperation) -> RetryPolicy:
    if operation is NetworkOperation.NON_IDEMPOTENT_WRITE:
        return NO_RETRY_POLICY
    if operation is NetworkOperation.IDEMPOTENT_WRITE:
        return WRITE_RETRY_POLICY
    return RetryPolicy(attempts=int(os.environ.get("CI_DOWNLOAD_RETRY_ATTEMPTS", "5")))

def retry_after_seconds(value: str | None, *, now: float | None = None) -> float | None:
    if not value: return None
    try: return max(0.0, float(value))
    except ValueError: pass
    try: parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError): return None
    if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - (time.time() if now is None else now))

def is_retryable_http(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES

def backoff_seconds(policy: RetryPolicy, retry_number: int, retry_after: float | None = None) -> float:
    base = min(policy.maximum_delay, policy.initial_delay * (2 ** max(0, retry_number - 1)))
    if retry_after is not None: base = max(base, min(policy.maximum_delay, retry_after))
    return base if policy.jitter == 0 else min(policy.maximum_delay, base + random.uniform(0, base * policy.jitter))

def _log(label: str, attempt: int, attempts: int, started: float, *, error: str | None = None, wait: float = 0) -> None:
    print(json.dumps({"network_operation": label, "attempt": attempt, "attempts": attempts, "error_class": error,
        "wait_seconds": round(wait, 3), "elapsed_seconds": round(time.monotonic() - started, 3)}, sort_keys=True), file=sys.stderr)

def request_with_retry(opener: Callable[..., object], request: object, *, timeout: float,
    operation: NetworkOperation | None = None, policy: RetryPolicy | None = None,
    idempotency_key: str | None = None, verify_postcondition: Callable[[], bool] | None = None,
    allow_write_retry: bool = False, sleep: Callable[[float], None] = time.sleep) -> object:
    method = str(getattr(request, "method", "GET") or "GET").upper()
    if operation is None:
        operation = NetworkOperation.READ if method in {"GET", "HEAD", "OPTIONS"} else (NetworkOperation.IDEMPOTENT_WRITE if allow_write_retry else NetworkOperation.NON_IDEMPOTENT_WRITE)
    if operation is NetworkOperation.IDEMPOTENT_WRITE and not (idempotency_key or verify_postcondition or allow_write_retry):
        raise ValueError("idempotent writes require an idempotency key or post-condition")
    effective = policy or policy_for(operation)
    if operation is NetworkOperation.NON_IDEMPOTENT_WRITE: effective = NO_RETRY_POLICY
    started = time.monotonic()
    for attempt in range(1, effective.attempts + 1):
        try:
            result = opener(request, timeout=timeout); _log(operation.value, attempt, effective.attempts, started); return result
        except HTTPError as exc:
            if verify_postcondition and verify_postcondition(): _log(operation.value, attempt, effective.attempts, started); return None
            if exc.code not in effective.retryable_statuses or attempt >= effective.attempts: raise
            retry_after, error = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None), f"http_{exc.code}"
        except (URLError, TimeoutError, ConnectionError) as exc:
            if verify_postcondition and verify_postcondition(): _log(operation.value, attempt, effective.attempts, started); return None
            if attempt >= effective.attempts: raise
            retry_after, error = None, type(exc).__name__
        wait = backoff_seconds(effective, attempt, retry_after); _log(operation.value, attempt, effective.attempts, started, error=error, wait=wait); sleep(wait)
    raise RuntimeError("unreachable retry state")

_SAFE_COMMANDS = (("git", "fetch"), ("go", "mod", "download"), ("cargo", "fetch"), ("pnpm", "fetch"),
    ("pnpm", "exec", "playwright", "install"), ("kubectl", "version"), ("kubectl", "auth", "can-i"))

def run_with_retry(argv: Sequence[str], operation: NetworkOperation, policy: RetryPolicy | None, label: str,
    *, sleep: Callable[[float], None] = time.sleep) -> subprocess.CompletedProcess[str]:
    if not argv or isinstance(argv, (str, bytes)): raise ValueError("command must be a non-empty argv sequence")
    command = tuple(argv)
    if operation not in {NetworkOperation.READ, NetworkOperation.DOWNLOAD} or not any(command[:len(prefix)] == prefix for prefix in _SAFE_COMMANDS):
        raise ValueError("command is not registered as a safe read/download operation")
    effective, started = policy or policy_for(operation), time.monotonic()
    for attempt in range(1, effective.attempts + 1):
        result = subprocess.run(list(command), text=True)
        if result.returncode == 0: _log(label, attempt, effective.attempts, started); return result
        if attempt >= effective.attempts: raise subprocess.CalledProcessError(result.returncode, list(command))
        wait = backoff_seconds(effective, attempt); _log(label, attempt, effective.attempts, started, error=f"exit_{result.returncode}", wait=wait); sleep(wait)
    raise RuntimeError("unreachable retry state")
