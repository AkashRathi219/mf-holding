"""In-process per-client rate limiting [H1].

Sliding-window counters keyed by client identity, safe for FastAPI's sync
threadpool endpoints (lock-protected), with bounded memory (idle keys are
pruned once over capacity). Reusable anywhere: auth endpoints today, the
public Try App upload endpoint tomorrow.

Proxy handling [BUG-H1]: reverse proxies APPEND to X-Forwarded-For, so the
leftmost entry is client-spoofable. We trust only the N rightmost entries,
where N = TRUSTED_PROXY_HOPS (env, default 0 = ignore XFF entirely and use
the socket peer). Set TRUSTED_PROXY_HOPS=1 on Railway-style single-proxy
deployments so the real client IP (appended by the platform) is used.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


def _trusted_hops() -> int:
    try:
        return max(0, int(os.environ.get("TRUSTED_PROXY_HOPS", "0")))
    except ValueError:
        return 0


def client_ip(request) -> str:
    peer = getattr(getattr(request, "client", None), "host", "") or "unknown"
    hops = _trusted_hops()
    if hops <= 0:
        return peer
    xff = request.headers.get("X-Forwarded-For", "")
    if not xff:
        return peer
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    # Take the rightmost entry added by the trusted proxy chain; anything
    # further left was chosen by the client.
    if not parts:
        return peer
    idx = len(parts) - hops
    return parts[idx] if idx >= 0 else peer


class SlidingWindowRateLimiter:
    """max_events per window_seconds per key; raises RateLimitExceeded."""

    # [BUG-M2] opportunistic prune runs on EVERY check once the key map grows
    # past this soft cap; the old reject-only pruning let idle keys pile up
    # without bound under sustained sub-limit traffic.
    PRUNE_ABOVE = 10_000

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
            if len(self._hits) > self.PRUNE_ABOVE:
                dead = [k for k, q in self._hits.items() if not q or q[-1] < cutoff]
                for k in dead:
                    del self._hits[k]
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_events:
                retry_after = int(round(dq[0] + self.window - now)) or 1
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
