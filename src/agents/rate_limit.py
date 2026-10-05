"""Per-host rate limiter + circuit-breaker facade for the AMC agents (SPEC §4, §8.2; AC-6; PLAN T6).

Three layers behind one object:

1. **Politeness spacing (in memory).** A host may not START a new request until
   ``MIN_SPACING_SECONDS`` (1.5 s, SPEC §4 "≥1.5s delay per domain") have passed
   since its previous request START - the moment the previous permit was
   granted - not since its release, so a fast request can never let the next
   one fire early.
2. **Per-host concurrency (in memory).** At most ``MAX_CONCURRENT_PER_HOST``
   (2, SPEC §4) permits may be in flight per host at any moment. All politeness
   state is keyed strictly by host: a busy or slow host never throttles a
   different host.
3. **Durable circuit breaker (facade).** :meth:`RateLimiter.record_block`,
   ``record_success``, ``is_blocked`` and ``remaining`` delegate to
   :class:`src.agents.state.AgentState`, which persists absolute UTC
   ``blocked_until`` timestamps per host and survives process restarts
   (SPEC §8.2: two consecutive HTTP 429 / Cloudflare ``ERR_WAF_CLOUDFLARE_1015``
   / 5xx events open a 30-minute lock). The 1800 s window is re-exported from
   ``state.BACKOFF_SECONDS``; this module never defines a competing literal.

Refusal is signalled by typed exceptions, never by a half-valid permit:
:class:`HostBlocked` when the breaker window is open (AC-6 demands ZERO
requests dispatched to that host, and an exception cannot be accidentally used
as a permit), :class:`HostBusy` when spacing or concurrency is not satisfied.
Both derive from :class:`RateLimitRefused`.

Clock and sleeps are injectable. ``now=`` is a zero-argument callable returning
epoch seconds (default ``time.time``); tests pass a fake clock.
``acquire``/``acquire_async`` never wait by default - they refuse immediately,
so tests pay for no sleeping. Production callers opt into waiting with
``wait=True`` and may inject the sleeper per call: ``sleep=`` (sync callable)
for ``acquire``/``slot``, an awaitable for ``acquire_async`` (default
``asyncio.sleep``). The waiter sleeps exactly the remaining delay and
re-checks the breaker after every nap, so a host that becomes blocked while
waiting is refused before any request is dispatched. The sync ``wait=True``
path on a concurrency-full host only unblocks if another thread releases a
permit; the asyncio path is the intended production wait.

The facade converts the float clock to the ISO-8601 UTC strings ``AgentState``
persists via ``time.gmtime`` formatting, keeping this module's imports inside
the stdlib set (asyncio / collections.abc / dataclasses / logging / time) while
breaker timestamps stay absolute-UTC and durable. The limiter is deliberately
not thread-safe: it serves the single-event-loop runner and coordinator
(PLAN §3). The breaker gates NEW acquisitions only; permits already in flight
complete normally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.agents.state import BACKOFF_SECONDS, DEFAULT_ROOT, AgentState, _clean_host
from src.agents.taxonomy import FAILURE_METADATA

logger = logging.getLogger(__name__)

MIN_SPACING_SECONDS = 1.5
MAX_CONCURRENT_PER_HOST = 2
WAIT_POLL_SECONDS = 0.05
WAF_1015_CODE = FAILURE_METADATA["ERR_WAF_CLOUDFLARE_1015"].code
DEFAULT_STATE_MF_ID = "shared"

__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_STATE_MF_ID",
    "MAX_CONCURRENT_PER_HOST",
    "MIN_SPACING_SECONDS",
    "WAIT_POLL_SECONDS",
    "WAF_1015_CODE",
    "HostBlocked",
    "HostBusy",
    "Permit",
    "RateLimitRefused",
    "RateLimiter",
    "is_blocking",
    "normalize_block_code",
]


def _iso_utc(epoch_seconds: float) -> str:
    """Format epoch seconds as the absolute ISO-8601 UTC string AgentState persists."""
    stamp = time.gmtime(epoch_seconds)
    return (
        f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}-{stamp.tm_mday:02d}"
        f"T{stamp.tm_hour:02d}:{stamp.tm_min:02d}:{stamp.tm_sec:02d}+00:00"
    )


def is_blocking(code: object) -> bool:
    """True for the SPEC §4/§8.2 blocking events: HTTP 429, any 5xx, Cloudflare 1015.

    Accepts an int status, a string status ("429", "503", "5XX") or the
    ``ERR_WAF_CLOUDFLARE_1015`` taxonomy code (case-insensitive).
    """
    if code is None or isinstance(code, bool):
        return False
    if isinstance(code, int):
        return code == 429 or code == 1015 or 500 <= code <= 599
    text = str(code).strip().upper()
    if not text:
        return False
    if text == WAF_1015_CODE or text in ("429", "1015", "5XX"):
        return True
    try:
        number = int(text)
    except ValueError:
        return False
    return number == 429 or number == 1015 or 500 <= number <= 599


def normalize_block_code(code: object) -> str | None:
    """Evidence string persisted by AgentState; an int 1015 becomes the taxonomy code."""
    if code is None:
        return None
    if isinstance(code, int) and not isinstance(code, bool):
        return WAF_1015_CODE if code == 1015 else str(code)
    return str(code)


class RateLimitRefused(Exception):
    """Base class for refused acquisitions (breaker or politeness)."""

    def __init__(self, host: str, message: str | None = None) -> None:
        self.host = host
        super().__init__(message if message is not None else f"request refused for host {host!r}")


class HostBlocked(RateLimitRefused):
    """The host's durable 30-minute breaker window is open (AC-6: zero requests)."""

    def __init__(self, host: str, remaining_seconds: float) -> None:
        self.remaining_seconds = remaining_seconds
        super().__init__(
            host,
            f"host {host!r} is circuit-broken for another {remaining_seconds:.1f}s "
            "(SPEC §8.2 / AC-6): zero requests until the window elapses",
        )


class HostBusy(RateLimitRefused):
    """Politeness refusal: spacing not elapsed or the host's concurrency is full."""

    def __init__(self, host: str, *, reason: str, retry_after: float) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(host, f"host {host!r} is busy ({reason}); retry after {retry_after:.3f}s")


@dataclass
class _HostSlot:
    """In-memory politeness slot for one host."""

    last_start: float | None = None
    in_flight: int = 0


class Permit:
    """One granted request slot for a host.

    ``started_at`` is the epoch time the request STARTED (the grant moment) -
    the reference for the host's next spacing window. ``release()`` frees the
    host's concurrency count and is idempotent; the permit is also a context
    manager that releases on exit.
    """

    __slots__ = ("_limiter", "_released", "host", "started_at")

    def __init__(self, limiter: RateLimiter, host: str, started_at: float) -> None:
        self._limiter = limiter
        self.host = host
        self.started_at = started_at
        self._released = False

    @property
    def released(self) -> bool:
        """True once :meth:`release` has run (idempotent afterwards)."""
        return self._released

    def release(self) -> None:
        """Free the host's concurrency count; extra calls are no-ops."""
        if self._released:
            return
        self._released = True
        self._limiter.release(self.host)

    def __enter__(self) -> Permit:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"Permit(host={self.host!r}, started_at={self.started_at:.3f}, released={self._released})"


class RateLimiter:
    """Per-host politeness (spacing + concurrency) with a durable breaker facade.

    Politeness state is in-memory and per-instance - share one instance across
    the agents that must co-ordinate on the same hosts. The breaker half
    delegates to the injected :class:`src.agents.state.AgentState` (default: a
    limiter-owned ``data/logs/agent_state/shared.json``); callers running
    per-AMC agents pass that agent's ``AgentState`` so breaker timestamps stay
    durable in the agent's own state file (SPEC §7).
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        state: AgentState | None = None,
        spacing: float = MIN_SPACING_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_PER_HOST,
    ) -> None:
        self._now = now if now is not None else time.time
        if not callable(self._now):
            raise TypeError("now must be a zero-argument callable returning epoch seconds")
        if spacing <= 0:
            raise ValueError(f"spacing must be positive, got {spacing!r}")
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent!r}")
        self.spacing = float(spacing)
        self.max_concurrent = int(max_concurrent)
        self.state = state if state is not None else AgentState(DEFAULT_STATE_MF_ID, root=DEFAULT_ROOT)
        self._slots: dict[str, _HostSlot] = {}

    def __repr__(self) -> str:
        return (
            f"RateLimiter(spacing={self.spacing!r}, max_concurrent={self.max_concurrent!r}, "
            f"hosts={len(self._slots)})"
        )

    def acquire(
        self,
        host: str,
        *,
        wait: bool = False,
        sleep: Callable[[float], None] | None = None,
    ) -> Permit:
        """Grant one request slot for ``host`` or refuse with a typed exception.

        Raises :class:`HostBlocked` while the host's 30-minute breaker window is
        open - checked first, before any waiting, so a blocked host is never
        slept into - and :class:`HostBusy` when the spacing has not elapsed
        since the host's last request START or ``max_concurrent`` permits are
        already in flight. With ``wait=True`` the caller sleeps (via the
        injectable ``sleep``, default ``time.sleep``) exactly the remaining
        delay and the breaker is re-checked after every nap; the default
        ``wait=False`` refuses immediately so tests never pay for sleeping.
        """
        host_s = _clean_host(host)
        while True:
            now_value = self._now()
            blocked_remaining = self._blocked_remaining(host_s, now_value)
            if blocked_remaining > 0.0:
                logger.debug("rate_limit: refused %s (breaker, %.1fs left)", host_s, blocked_remaining)
                raise HostBlocked(host_s, blocked_remaining)
            refusal = self._refusal(host_s, now_value)
            if refusal is None:
                return self._grant(host_s, now_value)
            if not wait:
                logger.debug("rate_limit: refused %s (%s)", host_s, refusal.reason)
                raise refusal
            sleeper = sleep if sleep is not None else time.sleep
            sleeper(max(refusal.retry_after, 0.0))

    async def acquire_async(
        self,
        host: str,
        *,
        wait: bool = False,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> Permit:
        """Awaitable :meth:`acquire`; the wait yields via the injectable sleeper.

        Same refusal contract as :meth:`acquire`. With ``wait=True`` the caller
        awaits ``sleep`` (default ``asyncio.sleep``) exactly the remaining
        delay, yielding to the event loop so other coroutines can release
        their permits, and the breaker is re-checked after every nap.
        """
        host_s = _clean_host(host)
        while True:
            now_value = self._now()
            blocked_remaining = self._blocked_remaining(host_s, now_value)
            if blocked_remaining > 0.0:
                logger.debug("rate_limit: refused %s (breaker, %.1fs left)", host_s, blocked_remaining)
                raise HostBlocked(host_s, blocked_remaining)
            refusal = self._refusal(host_s, now_value)
            if refusal is None:
                return self._grant(host_s, now_value)
            if not wait:
                logger.debug("rate_limit: refused %s (%s)", host_s, refusal.reason)
                raise refusal
            awaiter = sleep if sleep is not None else asyncio.sleep
            await awaiter(max(refusal.retry_after, 0.0))

    def slot(
        self,
        host: str,
        *,
        wait: bool = False,
        sleep: Callable[[float], None] | None = None,
    ) -> Permit:
        """Context-manager form of :meth:`acquire`: ``with limiter.slot(host): ...``.

        The returned :class:`Permit` releases its concurrency count on ``with``
        exit (including on exception); use ``as permit`` for the grant time.
        """
        return self.acquire(host, wait=wait, sleep=sleep)

    def release(self, host: str) -> None:
        """Free one in-flight slot for ``host``; releasing with none is a no-op."""
        host_s = _clean_host(host)
        slot = self._slots.get(host_s)
        if slot is None or slot.in_flight <= 0:
            logger.debug("rate_limit: release ignored for %s (no permits in flight)", host_s)
            return
        slot.in_flight -= 1
        logger.debug("rate_limit: released %s (%d in flight)", host_s, slot.in_flight)

    def in_flight(self, host: str) -> int:
        """Current in-flight permit count for ``host`` (introspection/tests)."""
        slot = self._slots.get(_clean_host(host))
        return slot.in_flight if slot is not None else 0

    def is_blocked(self, host: str) -> bool:
        """True while the host's durable 30-minute breaker window is open."""
        return self._blocked_remaining(_clean_host(host), self._now()) > 0.0

    def remaining(self, host: str) -> float:
        """Seconds left on the host's breaker window; ``0.0`` when clear."""
        return self._blocked_remaining(_clean_host(host), self._now())

    def record_block(self, host: str, code: object = None) -> dict:
        """Record one blocking event for ``host`` in the durable breaker state.

        ``code`` is the caller's classification - an HTTP status (429, any 5xx,
        Cloudflare 1015) or the ``ERR_WAF_CLOUDFLARE_1015`` taxonomy code; a
        non-blocking code raises ``ValueError`` so politeness failures can
        never pollute the breaker counter. ``None`` records a pre-classified
        event. Delegates to :meth:`AgentState.record_block`, which trips the
        30-minute lock on the threshold (SPEC §8.2).
        """
        if code is not None and not is_blocking(code):
            raise ValueError(
                f"code {code!r} is not a blocking event (HTTP 429, 5xx or {WAF_1015_CODE}); "
                "classify per SPEC §6 before recording"
            )
        return self.state.record_block(host, code=normalize_block_code(code), now=_iso_utc(self._now()))

    def record_success(self, host: str) -> dict:
        """Record a successful interaction; resets the consecutive-block counter."""
        return self.state.record_success(host, now=_iso_utc(self._now()))

    def _blocked_remaining(self, host: str, now_value: float) -> float:
        return self.state.remaining_backoff(host, now=_iso_utc(now_value))

    def _refusal(self, host: str, now_value: float) -> HostBusy | None:
        slot = self._slots.get(host)
        if slot is not None and slot.in_flight >= self.max_concurrent:
            return HostBusy(host, reason="concurrent", retry_after=WAIT_POLL_SECONDS)
        if slot is not None and slot.last_start is not None:
            spacing_remaining = self.spacing - (now_value - slot.last_start)
            if spacing_remaining > 0.0:
                return HostBusy(host, reason="spacing", retry_after=spacing_remaining)
        return None

    def _grant(self, host: str, now_value: float) -> Permit:
        slot = self._slots.setdefault(host, _HostSlot())
        slot.last_start = now_value
        slot.in_flight += 1
        logger.debug("rate_limit: granted %s at %.3f (%d in flight)", host, now_value, slot.in_flight)
        return Permit(self, host, now_value)
