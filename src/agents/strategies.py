"""Strategy ladder orchestration for the AMC discovery agents (SPEC §5 step 3, PLAN T7).

The ladder is the fixed escalation order every AMC discovery run walks before
giving up: ``fast_http`` -> ``curl_impersonate`` ->
``playwright_token_intercept_then_api`` -> ``direct_api``. The rung names are
the same discrete actions the bandit (``src.agents.bandit``) selects among;
this module is the sequential executor that tries them in order until one
produces candidate document links.

This is an orchestration layer, NOT a scraper: all real work is injected via
the ``discover`` callable (``discover(strategy, amc_name) -> list``), so the
module imports nothing heavier than the failure taxonomy and tests run with
zero network. Production callers bind ``discover`` to the HybridAdapter /
curl_cffi / Playwright-token-intercept machinery that already exists in
``src/amc_adapters`` (plain httpx fast path, curl_cffi Chrome impersonation for
HDFC/Edelweiss, the Axis-style Playwright Bearer-token intercept replayed via
httpx, and direct documented API calls).

Policy implemented by :func:`run_ladder`:

1. Walk the ladder in order (``strategy_order`` may reorder it for tests),
   skipping ``skip`` rungs and attempting at most ``max_rungs`` rungs.
2. The FIRST rung whose ``discover`` returns a non-empty list wins; later
   rungs are never called.
3. If ``discover`` raises, the exception is mapped through
   ``taxonomy.classify_exception``. A WAF/429/1015 block code
   (``ERR_WAF_CLOUDFLARE_1015``) sets ``blocked=True`` and STOPS the ladder
   immediately - no hammering a host that is rate-limiting or banning the IP
   (SPEC §4/§8.2). Any other classified code is recorded and the ladder falls
   through to the next rung.
4. When a ``limiter`` (:class:`src.agents.rate_limit.RateLimiter`) is passed,
   ``limiter.is_blocked(host)`` is checked BEFORE any rung; an open breaker
   window returns immediately with ``blocked=True`` and zero discover calls
   (AC-6: zero requests to a circuit-broken host).
5. If nothing worked, ``success=False`` and ``failure_code`` is the last
   non-None code recorded across attempts, or ``DEFAULT_FAILURE_CODE`` when
   nothing was classified: the ladder's own failure mode is "no candidate
   document links reachable on any rung", which is the
   ``ERR_DOM_GATED_INLINE_URLS`` family in the fixed taxonomy - its remediation
   ladder (consent dismissal + regex sweep of the raw HTML) is exactly the
   next thing a caller should try.

Every rung tried is recorded in ``StrategyResult.attempts`` as
``{strategy, ok, failure_code, elapsed}`` so episodes and the playbook register
can score strategies per SPEC §5.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass

from src.agents.taxonomy import FAILURE_METADATA, classify_exception

logger = logging.getLogger(__name__)

STRATEGY_LADDER: tuple[str, ...] = (
    "fast_http",
    "curl_impersonate",
    "playwright_token_intercept_then_api",
    "direct_api",
)

STRATEGY_INFO: dict[str, str] = {
    "fast_http": (
        "Plain httpx GET of the AMC page and link sweep - the HybridAdapter "
        "fast path; cheapest rung, tried first."
    ),
    "curl_impersonate": (
        "curl_cffi Chrome TLS impersonation for Akamai/Cloudflare-fingerprinted "
        "hosts (HDFC, Edelweiss, NSE)."
    ),
    "playwright_token_intercept_then_api": (
        "Headless Playwright load captures the browser-issued Authorization: "
        "Bearer token, then the CMS API is replayed via httpx (Axis MF pattern)."
    ),
    "direct_api": (
        "Call the AMC's documented JSON endpoint directly with no browser - "
        "last rung for AMCs with a public API."
    ),
}

WAF_1015_CODE = FAILURE_METADATA["ERR_WAF_CLOUDFLARE_1015"].code
BLOCK_CODES = frozenset({WAF_1015_CODE})
DEFAULT_FAILURE_CODE = FAILURE_METADATA["ERR_DOM_GATED_INLINE_URLS"].code

DiscoverFn = Callable[[str, str], list]

__all__ = [
    "BLOCK_CODES",
    "DEFAULT_FAILURE_CODE",
    "STRATEGY_INFO",
    "STRATEGY_LADDER",
    "DiscoverFn",
    "StrategyResult",
    "WAF_1015_CODE",
    "run_ladder",
]


@dataclass
class StrategyResult:
    """Outcome of one :func:`run_ladder` walk.

    ``strategy`` is the winning rung (``""`` when no rung produced links),
    ``links`` the winning rung's candidate document links, ``attempts`` one
    ``{strategy, ok, failure_code, elapsed}`` dict per rung tried in order,
    ``failure_code`` the taxonomy code for the failure (``None`` on success)
    and ``blocked`` True when a WAF/429/1015 block or an open limiter breaker
    stopped the walk.
    """

    strategy: str
    links: list
    attempts: list[dict]
    success: bool
    failure_code: str | None
    blocked: bool = False


def run_ladder(
    amc_name: str,
    *,
    discover: DiscoverFn,
    strategy_order: Collection[str] | None = None,
    skip: Collection[str] = (),
    limiter: object | None = None,
    max_rungs: int | None = None,
    host: str | None = None,
) -> StrategyResult:
    """Walk the strategy ladder for ``amc_name`` and return the first hit.

    ``discover(strategy, amc_name) -> list`` does all real work and is
    injected; it must return the candidate document links for that rung (an
    empty list means the rung found nothing). ``strategy_order`` overrides the
    default ladder order, ``skip`` removes rungs entirely, ``max_rungs`` caps
    how many rungs may be attempted, and ``host`` (default ``amc_name``) is the
    key checked against the limiter's breaker. See the module docstring for
    the full stop/fall-through policy.
    """
    check_host = host or amc_name
    if limiter is not None and limiter.is_blocked(check_host):
        logger.warning("strategies: host %r is circuit-broken; ladder not started", check_host)
        return StrategyResult(
            strategy="", links=[], attempts=[], success=False,
            failure_code=WAF_1015_CODE, blocked=True,
        )

    rungs = tuple(strategy_order) if strategy_order is not None else STRATEGY_LADDER
    skipped = frozenset(skip)
    rungs = tuple(rung for rung in rungs if rung not in skipped)
    if max_rungs is not None:
        if max_rungs < 0:
            raise ValueError(f"max_rungs must be >= 0, got {max_rungs!r}")
        rungs = rungs[:max_rungs]

    attempts: list[dict] = []
    last_code: str | None = None
    for rung in rungs:
        started = time.perf_counter()
        try:
            links = discover(rung, amc_name)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            code = classify_exception(exc)
            attempts.append(
                {"strategy": rung, "ok": False, "failure_code": code, "elapsed": elapsed}
            )
            if code is not None:
                last_code = code
            if code in BLOCK_CODES:
                logger.warning(
                    "strategies: %s raised %s (%s) for %r - host blocked, ladder stopped",
                    rung, type(exc).__name__, code, amc_name,
                )
                return StrategyResult(
                    strategy="", links=[], attempts=attempts, success=False,
                    failure_code=code, blocked=True,
                )
            logger.debug("strategies: %s failed for %r (%s); falling through", rung, amc_name, code)
            continue
        elapsed = time.perf_counter() - started
        if links:
            attempts.append(
                {"strategy": rung, "ok": True, "failure_code": None, "elapsed": elapsed}
            )
            logger.info("strategies: %s produced %d link(s) for %r", rung, len(links), amc_name)
            return StrategyResult(
                strategy=rung, links=list(links), attempts=attempts, success=True,
                failure_code=None, blocked=False,
            )
        attempts.append(
            {"strategy": rung, "ok": False, "failure_code": None, "elapsed": elapsed}
        )
        logger.debug("strategies: %s found no links for %r; falling through", rung, amc_name)

    failure_code = last_code if last_code is not None else DEFAULT_FAILURE_CODE
    logger.info("strategies: ladder exhausted for %r (failure_code=%s)", amc_name, failure_code)
    return StrategyResult(
        strategy="", links=[], attempts=attempts, success=False,
        failure_code=failure_code, blocked=False,
    )
