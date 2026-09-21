"""Small, bounded transport helpers shared by CI control-plane clients.

The helper deliberately treats reads and downloads differently from writes.  A
caller must opt into retrying a write and provide an idempotency key or a
post-condition check; this prevents a transient network error from creating
duplicate releases or tasks.
"""

from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass
from datetime import timezone
from typing import Callable
from urllib.error import HTTPError, URLError


RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    initial_delay: float = 1.0
    maximum_delay: float = 30.0
    jitter: float = 0.25

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.initial_delay < 0 or self.maximum_delay < 0 or self.jitter < 0:
            raise ValueError("invalid retry policy")


READ_RETRY_POLICY = RetryPolicy()


def retry_after_seconds(value: str | None, *, now: float | None = None) -> float | None:
    """Parse either a Retry-After delta or an HTTP date."""

    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - (time.time() if now is None else now))


def is_retryable_http(status: int) -> bool:
    return status in RETRYABLE_STATUS_CODES


def backoff_seconds(policy: RetryPolicy, retry_number: int, retry_after: float | None = None) -> float:
    """Return a bounded exponential backoff with small random jitter."""

    base = min(policy.maximum_delay, policy.initial_delay * (2 ** max(0, retry_number - 1)))
    if retry_after is not None:
        base = max(base, min(policy.maximum_delay, retry_after))
    if policy.jitter == 0:
        return base
    return min(policy.maximum_delay, base + random.uniform(0, base * policy.jitter))


def request_with_retry(
    opener: Callable[..., object],
    request: object,
    *,
    timeout: float,
    policy: RetryPolicy = READ_RETRY_POLICY,
    allow_write_retry: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Call ``opener`` with bounded retries.

    ``allow_write_retry`` is intentionally false by default.  It is only
    appropriate for a caller that has an idempotency key or verifies a
    post-condition after a transport failure.
    """

    method = str(getattr(request, "method", "GET") or "GET").upper()
    retryable_method = method in {"GET", "HEAD", "OPTIONS"} or allow_write_retry
    last_error: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return opener(request, timeout=timeout)
        except HTTPError as exc:
            last_error = exc
            if not retryable_method or not is_retryable_http(exc.code) or attempt >= policy.attempts:
                raise
            retry_after = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
        except (URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if not retryable_method or attempt >= policy.attempts:
                raise
            retry_after = None
        sleep(backoff_seconds(policy, attempt, retry_after))
    assert last_error is not None
    raise last_error
