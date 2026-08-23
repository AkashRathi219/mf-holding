"""In-process per-client rate limiting [H1].

Sliding-window counters keyed by client identity, safe for FastAPI's sync
threadpool endpoints (lock-protected), with bounded memory (idle keys are
pruned once over capacity). Reusable anywhere: auth endpoints today, the
public Try App upload endpoint tomorrow.

Railway terminates TLS in front of us, so the real client IP arrives in
X-Forwarded-For; fall back to request.client.host.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


def client_ip(request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


class SlidingWindowRateLimiter:
    """max_events per window_seconds per key; raises RateLimitExceeded."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record one event for `key` or raise RateLimitExceeded."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_events:
                retry_after = int(round(dq[0] + self.window - now)) or 1
                # Opportunistic prune: drop fully-expired keys when the map
                # grows past 2x its live population estimate.
                if len(self._hits) > 10_000:
                    dead = [k for k, q in self._hits.items() if not q or q[-1] < cutoff]
                    for k in dead:
                        del self._hits[k]
                raise RateLimitExceeded(retry_after)
            dq.append(now)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_s: int):
        super().__init__(f"Rate limit exceeded; retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


# Shared limiters (module-level so all workers of one process share them).
AUTH_LOGIN_LIMITER = SlidingWindowRateLimiter(max_events=10, window_seconds=300)
AUTH_REGISTER_LIMITER = SlidingWindowRateLimiter(max_events=5, window_seconds=3600)


def enforce(limiter: SlidingWindowRateLimiter, request) -> None:
    """Raise HTTPException(429) when the limiter rejects this request."""
    from fastapi import HTTPException

    try:
        limiter.check(client_ip(request))
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again shortly.",
            headers={"Retry-After": str(e.retry_after_s)},
        )
