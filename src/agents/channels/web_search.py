"""Channel 2 ``web_search`` - allow-list-gated web-search candidate resolution
(SPEC §11.3 channel 2, AC-19, OQ-10, T34).

For an escalated ticket ``(amc, scheme, month)`` this channel proposes search
queries for that scheme-month's FULL portfolio disclosure, resolves them via
a pluggable search provider (OQ-10) and fetches/parses the resulting
candidate documents - but ONLY on hosts the AC-19 allow-list trusts.

The allow-list (AC-19) is derived from:

1. every official AMC host parseable from ``config/amc_registry.json`` (the
   ``amc_monthly_mf_factsheets`` / ``amc_monthly_portfolio_disclosure`` /
   ``amc_fortnightly_portfolio_disclosure`` / ``scheme_wise`` URL fields; 53
   of the 57 registry entries carry at least one URL, the rest contribute
   none),
2. ``amfiindia.com`` (subdomains included),
3. the known Advisorkhoj republisher host ``advisorkhoj.com``.

Host comparison is exact-host/suffix-safe: an entry ``axismf.com`` allows
``axismf.com``, ``www.axismf.com`` (``www.`` is normalised away) and any
``*.axismf.com`` subdomain - and NOTHING else.  ``axismf.com.evil.example``,
``evilaxismf.com`` and every other look-alike are rejected; a candidate on a
non-allow-listed host is recorded as ``skipped_untrusted_host`` and is NEVER
passed to the fetcher (a false allow here is a security bug, so the gate is
deliberately strict and fails closed on malformed URLs and non-web schemes).

The provider is pluggable (OQ-10): :class:`SearchProvider` is the protocol,
:class:`NullSearchProvider` the default no-network implementation.  With no
provider configured the channel reports ``skipped_no_provider`` and the
ladder proceeds to the AMFI channel.  The channel module itself performs no
network I/O: provider, fetcher and parser are all injected, so tests run on
fakes only.

Escalation citizenship (OQ-7): the channel is a pure worker - it never
mutates the escalation queue or the DB, walks its candidates strictly
sequentially, and NEVER evades a block: an injected ``is_blocked`` host guard
(a circuit-breaker hook, §8) stops the walk immediately and is reported.
The channel is delay-free by design - the rate limiter owns pacing.  Any
exception is mapped through ``src.agents.taxonomy.classify_exception``:
codes are never invented and a failed result never carries
``failure_code=None``.

Success requires a FULL-disclosure document (``src.document_class.classify``)
whose holding set reaches Σ >= 95% (``src.agents.tiers.classify`` -> tier T0
``COMPLETE_100``).  A factsheet top-10 does NOT close the ticket - it is an
accepted T1 elsewhere and is only recorded as a fallback observation with
``success=False``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from src.agents import taxonomy
from src.agents.channels import ChannelResult, evaluate_candidate_payload
from src.agents.channels.amc_recheck import (
    Blocked,
    PARSE_STRATEGIES,
    _candidate_paths,
    _extract_scheme_holdings,
    pick_alternate_strategy,
)
from src.agents.escalation import Ticket

logger = logging.getLogger(__name__)

CHANNEL = "web_search"

WAF_CODE = "ERR_WAF_CLOUDFLARE_1015"
CODE_SUSPECT_SCALE = "ERR_SCALE_SUSPECT_WEIGHTS"
CODE_INCOMPLETE_SUM = "ERR_HOLDINGS_INCOMPLETE_SUM"
CODE_TOPN_ONLY = "ERR_HOLDINGS_TOPN_ONLY"
CODE_PARSER_PARTIAL = "ERR_HOLDINGS_PARSER_PARTIAL"
CODE_MISSING = "ERR_SCHEME_MISSING_IN_DB"

_CHANNEL_CODES = (
    WAF_CODE,
    CODE_SUSPECT_SCALE,
    CODE_INCOMPLETE_SUM,
    CODE_TOPN_ONLY,
    CODE_PARSER_PARTIAL,
    CODE_MISSING,
)
for _code in _CHANNEL_CODES:
    if not taxonomy.is_valid_code(_code):
        raise RuntimeError(f"web_search references unknown taxonomy code {_code!r}")

# Outcome codes ranked most-informative first; the walk reports the
# highest-ranked outcome it observed (ties keep the first occurrence).
_FAILURE_PRIORITY: dict[str, int] = {
    CODE_SUSPECT_SCALE: 4,
    CODE_INCOMPLETE_SUM: 3,
    CODE_TOPN_ONLY: 2,
    CODE_PARSER_PARTIAL: 1,
    CODE_MISSING: 0,
}

REASON_SKIPPED_NO_PROVIDER = "skipped_no_provider"
REASON_SKIPPED_UNTRUSTED_HOST = "skipped_untrusted_host"

# ---------------------------------------------------------------------------
# AC-19 allow-list: official AMC hosts + AMFI + Advisorkhoj
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parents[3]
REGISTRY_PATH = BASE_DIR / "config" / "amc_registry.json"

AMFI_HOST = "amfiindia.com"
ADVISORKHOJ_HOST = "advisorkhoj.com"

REGISTRY_URL_FIELDS: tuple[str, ...] = (
    "amc_monthly_mf_factsheets",
    "amc_monthly_portfolio_disclosure",
    "amc_fortnightly_portfolio_disclosure",
    "scheme_wise",
)

# Only web URLs are fetchable candidates; anything else fails closed.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def normalize_host(host: object) -> str:
    """Lower-case ``host`` and strip its trailing dot and leading ``www.``."""
    text = str(host or "").strip().rstrip(".").casefold()
    if text.startswith("www."):
        text = text[len("www."):]
    return text


def build_allowlist(registry_path: str | Path = REGISTRY_PATH) -> frozenset[str]:
    """Derive the AC-19 allow-list entry set (one file read, no network).

    Entries are the normalized hosts of every registry URL field plus
    ``amfiindia.com`` and ``advisorkhoj.com``.  A missing or unreadable
    registry fails CLOSED: the entry set shrinks to the two statutory hosts
    and the problem is logged - it can never widen the gate.
    """
    entries: set[str] = {AMFI_HOST, ADVISORKHOJ_HOST}
    path = Path(registry_path)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            registry = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(
            "web_search allow-list: registry %s unreadable (%s); "
            "allow-list shrinks to the statutory hosts",
            path,
            exc,
        )
        return frozenset(entries)
    for entry in registry if isinstance(registry, list) else []:
        if not isinstance(entry, dict):
            continue
        for field in REGISTRY_URL_FIELDS:
            url = str(entry.get(field) or "").strip()
            if not url:
                continue
            try:
                host = urlsplit(url).hostname
            except ValueError:
                continue
            if host:
                entries.add(normalize_host(host))
    return frozenset(entries)


_ALLOWLIST_CACHE: frozenset[str] | None = None


def allowlist_hosts() -> set[str]:
    """The cached AC-19 allow-list entry set (normalized hosts)."""
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is None:
        _ALLOWLIST_CACHE = build_allowlist()
    return set(_ALLOWLIST_CACHE)


def reset_allowlist_cache() -> None:
    """Drop the cache so the next ``allowlist_hosts()`` re-reads the registry."""
    global _ALLOWLIST_CACHE
    _ALLOWLIST_CACHE = None


def _url_parts(url: object) -> tuple[str, str]:
    """(scheme, normalized host) of ``url``; ``("", "")`` when unusable.

    ``urlsplit(...).hostname`` (not the raw netloc) is used so userinfo
    tricks like ``https://axismf.com@evil.example/`` resolve to the REAL
    connect host (``evil.example``) and are judged accordingly.
    """
    text = str(url or "").strip()
    if not text:
        return "", ""
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return "", ""
    if not host:
        return "", ""
    return parts.scheme.casefold(), normalize_host(host)


def host_of(url: object) -> str:
    """Normalized hostname of ``url``; ``""`` when malformed or hostless."""
    return _url_parts(url)[1]


def is_allowed(
    url: object,
    *,
    allowlist: Collection[str] | None = None,
) -> bool:
    """AC-19 gate: True only for a web URL on an allow-listed host/subdomain.

    An entry ``axismf.com`` allows exactly ``axismf.com``,
    ``www.axismf.com`` (``www.`` normalizes away) and ``*.axismf.com`` -
    and nothing else: ``axismf.com.evil.example``, ``evilaxismf.com`` and
    every other look-alike fail the exact-host/suffix match.  Malformed
    URLs, hostless URLs and non-web schemes fail closed.  A false allow
    here would be a security bug; a false reject merely skips one candidate.
    """
    scheme, host = _url_parts(url)
    if not host or scheme not in ALLOWED_SCHEMES:
        return False
    entries = allowlist if allowlist is not None else allowlist_hosts()
    for entry in entries:
        if host == entry or host.endswith("." + entry):
            return True
    return False


# ---------------------------------------------------------------------------
# Pluggable search provider (OQ-10)
# ---------------------------------------------------------------------------


class SearchProvider(Protocol):
    """Pluggable web-search provider (OQ-10).

    The channel calls ``search(query, limit=N)`` for every planned query and
    expects an ordered iterable of candidate URL strings (possibly empty).
    Implementations own the actual network search; this module never touches
    the network itself.  Raise on provider failure - the channel maps the
    exception through the taxonomy.
    """

    def search(self, query: str, *, limit: int = 10) -> Sequence[str]: ...


class NullSearchProvider:
    """Default no-network provider: every query resolves to zero candidates."""

    def search(self, query: str, *, limit: int = 10) -> Sequence[str]:
        return []


def _search_fn(provider: object) -> object:
    search = getattr(provider, "search", None)
    if callable(search):
        return search
    if callable(provider):
        return provider
    raise TypeError(
        "provider must expose search(query, *, limit) or be a callable "
        f"(got {type(provider).__name__})"
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One proposed search query for the ticket's full portfolio disclosure."""

    query: str


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _month_label(month: str) -> str:
    parts = month.split("-")
    if len(parts) >= 2:
        try:
            return f"{_MONTH_NAMES[int(parts[1]) - 1]} {int(parts[0])}"
        except (ValueError, IndexError):
            pass
    return month


def plan(ticket: Ticket) -> list[Candidate]:
    """Ordered, deterministic search queries for ``ticket`` (pure, no I/O).

    Three query shapes cover the corpus's naming variance: the raw ``YYYY-MM``
    token, the ``Month YYYY`` spelling, and an AMC-level query for schemes the
    search engines index poorly.
    """
    amc = " ".join(str(ticket.amc or "").split())
    scheme = " ".join(str(ticket.scheme or "").split())
    month = " ".join(str(ticket.month or "").split())
    label = _month_label(month)
    queries = [
        " ".join(p for p in (amc, scheme, "monthly portfolio disclosure", month) if p),
        " ".join(p for p in (amc, scheme, "portfolio disclosure", label) if p),
        " ".join(p for p in (amc, "monthly portfolio statement", label) if p),
    ]
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            candidates.append(Candidate(query=query))
    return candidates


# ---------------------------------------------------------------------------
# Execution (all I/O injected)
# ---------------------------------------------------------------------------


def _waf_result(
    subject: str,
    new_paths: list[str],
    code: str,
    *,
    via: str,
    strategy_used: str,
) -> ChannelResult:
    return ChannelResult(
        channel=CHANNEL,
        success=False,
        reason=(
            f"WAF block ({code}) reported via {via} for {subject}; "
            f"stopped without evading, rotating IPs or retrying"
        ),
        new_document_paths=list(new_paths),
        strategy_used=strategy_used,
        failure_code=code,
    )


def run(
    ticket: Ticket,
    *,
    provider: object | None = None,
    fetcher: object | None = None,
    parse: object | None = None,
    session: object = None,
    candidates: Sequence[Candidate] | None = None,
    failed_strategy: str | None = None,
    is_blocked: object = None,
    limit: int = 10,
    now: object = None,
) -> ChannelResult:
    """Resolve planned search queries and report one :class:`ChannelResult`.

    Injected dependencies (no real network, no filesystem access of its own):

    * ``provider`` - a :class:`SearchProvider` (object exposing
      ``search(query, *, limit)``) or a plain callable of the same shape;
      ``None`` short-circuits the channel with ``skipped_no_provider``
      (OQ-10) so the ladder proceeds to the AMFI channel.
    * ``fetcher(url, ticket, session)`` -> ordered document paths for one
      candidate URL, a :class:`Blocked` sentinel when the host blocked, or
      ``[]`` when nothing was obtained.  The fetcher is called ONLY for URLs
      that pass :func:`is_allowed` (AC-19) and only when the injected
      ``is_blocked`` host guard does not trip first.
    * ``parse(document_path, parse_strategy, session)`` -> parsed payload dict
      in one of the repo's parsed shapes (see
      ``amc_recheck._extract_scheme_holdings``); raises on parse failure.
    * ``session`` is opaque context (e.g. an httpx client) passed through.
    * ``candidates`` overrides :func:`plan`'s queries.
    * ``failed_strategy`` seeds the parse rotation away from the strategy
      that produced the incomplete set (the AC-18 forced-alternate spirit).
    * ``is_blocked(host)`` - optional circuit-breaker guard over the
      normalized candidate host; a truthy result stops the walk immediately
      (no fetch, no evasion) and is reported.
    * ``now`` is accepted for caller uniformity (episode stamping); the
      channel itself is clock-free and delay-free - the rate limiter owns
      pacing (§8).

    The walk is strictly sequential (OQ-7), resolves each URL at most once,
    stops immediately on a block (no evasion, no retry storm), and never
    mutates the escalation queue or the DB.  Success requires a
    full-disclosure document (``document_class.classify``) reaching Σ >= 95
    (``tiers.classify`` -> ``COMPLETE_100``); a factsheet top-10 is recorded
    as a fallback observation with ``success=False``.
    """
    if provider is None:
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=(
                f"{REASON_SKIPPED_NO_PROVIDER}: no search provider configured; "
                f"ladder proceeds to the next channel (OQ-10)"
            ),
            strategy_used="",
            failure_code=CODE_MISSING,
        )
    if fetcher is None or parse is None:
        raise ValueError("web_search.run requires injected fetcher and parse callables")
    if is_blocked is not None and not callable(is_blocked):
        raise TypeError("is_blocked must be a callable(host) -> bool")

    search = _search_fn(provider)
    planned = list(candidates) if candidates is not None else plan(ticket)
    failed_name = str(failed_strategy or "").strip()
    start = pick_alternate_strategy(failed_name)
    rotation = [start] + [s for s in PARSE_STRATEGIES if s != start]

    new_paths: list[str] = []
    seen_paths: set[str] = set()
    seen_urls: set[str] = set()
    observations: list[str] = []
    untrusted: list[str] = []
    best: tuple[int, str, str] | None = None
    strategy_used = ""

    def note(code: str, reason: str) -> None:
        nonlocal best
        priority = _FAILURE_PRIORITY.get(code, -1)
        if best is None or priority > best[0]:
            best = (priority, code, reason)

    def with_untrusted(reason: str) -> str:
        if not untrusted:
            return reason
        hosts = ", ".join(sorted(set(untrusted)))
        suffix = (
            f"{REASON_SKIPPED_UNTRUSTED_HOST}: {len(untrusted)} candidate URL(s) on "
            f"non-allow-listed host(s) [{hosts}] never fetched (AC-19)"
        )
        return f"{reason}; {suffix}" if reason else suffix

    for index, candidate in enumerate(planned):
        parse_strategy = rotation[index % len(rotation)]
        strategy_used = parse_strategy
        if not isinstance(candidate, Candidate) or not str(candidate.query).strip():
            observations.append(f"candidate {index + 1}: empty query - skipped")
            continue
        try:
            resolved = search(candidate.query, limit=limit)
        except Exception as exc:
            code = taxonomy.classify_exception(exc)
            if code == WAF_CODE:
                return _waf_result(
                    candidate.query,
                    new_paths,
                    code,
                    via="search provider",
                    strategy_used=strategy_used,
                )
            code = code or CODE_MISSING
            note(
                code,
                f"search provider failed on query {index + 1} ({code}); "
                f"no candidates resolved",
            )
            observations.append(f"query {index + 1}: provider failed ({code})")
            continue
        urls = [str(u).strip() for u in (resolved or []) if str(u).strip()]
        if not urls:
            observations.append(f"query {index + 1}: no candidate URLs from provider")
            note(
                CODE_MISSING,
                "no candidate URLs resolved from any search query ("
                + "; ".join(observations)
                + ")",
            )
            continue

        for url in urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            scheme, host = _url_parts(url)
            if not host:
                observations.append(
                    f"candidate {index + 1}: malformed URL (no host) - not fetched"
                )
                continue
            if is_blocked is not None:
                try:
                    blocked = bool(is_blocked(host))
                except Exception as exc:
                    code = taxonomy.classify_exception(exc) or CODE_MISSING
                    return ChannelResult(
                        channel=CHANNEL,
                        success=False,
                        reason=(
                            f"host guard failed for {host} ({code}); "
                            f"stopped fail-closed without fetching"
                        ),
                        new_document_paths=list(new_paths),
                        strategy_used=strategy_used,
                        failure_code=code,
                    )
                if blocked:
                    return _waf_result(
                        host,
                        new_paths,
                        WAF_CODE,
                        via="host guard",
                        strategy_used=strategy_used,
                    )
            if not is_allowed(url):
                untrusted.append(host)
                continue
            try:
                fetched = fetcher(url, ticket, session)
            except Exception as exc:
                code = taxonomy.classify_exception(exc)
                if code == WAF_CODE:
                    return _waf_result(
                        host,
                        new_paths,
                        code,
                        via="fetcher",
                        strategy_used=strategy_used,
                    )
                code = code or CODE_MISSING
                note(code, f"fetch failed for {host} ({code}); document not obtained")
                observations.append(f"{host}: fetch failed ({code})")
                continue
            if isinstance(fetched, Blocked):
                code = fetched.code if taxonomy.is_valid_code(fetched.code) else WAF_CODE
                return _waf_result(
                    host,
                    new_paths,
                    code,
                    via="fetcher sentinel",
                    strategy_used=strategy_used,
                )
            paths = _candidate_paths(fetched)
            if not paths:
                observations.append(f"{host}: no document returned")
                note(
                    CODE_MISSING,
                    "no document obtained from any allow-listed candidate ("
                    + "; ".join(observations)
                    + ")",
                )
                continue

            for path in paths:
                if path not in seen_paths:
                    seen_paths.add(path)
                    new_paths.append(path)
                try:
                    payload = parse(path, parse_strategy, session)
                except Exception as exc:
                    code = taxonomy.classify_exception(exc)
                    if code == WAF_CODE:
                        return _waf_result(
                            host,
                            new_paths,
                            code,
                            via="parser",
                            strategy_used=strategy_used,
                        )
                    code = code or CODE_PARSER_PARTIAL
                    note(
                        code,
                        f"parse failed on {Path(path).name} from {host} "
                        f"(parse:{parse_strategy}): {code}",
                    )
                    continue
                if not isinstance(payload, Mapping) or not payload:
                    note(
                        CODE_PARSER_PARTIAL,
                        f"empty parse of {Path(path).name} from {host} "
                        f"(parse:{parse_strategy})",
                    )
                    continue

                verdict = evaluate_candidate_payload(payload, ticket.scheme, source_file=str(path))

                if verdict.is_t0:
                    return ChannelResult(
                        channel=CHANNEL,
                        success=True,
                        reason=with_untrusted(
                            f"closed by {verdict.document_class} document {Path(path).name} "
                            f"from {host} (parse:{parse_strategy}): {verdict.tier.reason}"
                        ),
                        new_document_paths=list(new_paths),
                        strategy_used=parse_strategy,
                        failure_code=None,
                    )
                if verdict.failure_code == CODE_SUSPECT_SCALE:
                    note(
                        CODE_SUSPECT_SCALE,
                        f"suspect scale in {Path(path).name} from {host} "
                        f"(parse:{parse_strategy}): {verdict.tier.reason}",
                    )
                elif verdict.failure_code == CODE_TOPN_ONLY:
                    note(
                        CODE_TOPN_ONLY,
                        f"only a factsheet_topn document ({Path(path).name} from {host}, "
                        f"Σ={verdict.tier.coverage_pct:.2f}%) (parse:{parse_strategy}) - "
                        f"accepted T1 elsewhere, does NOT close this ticket",
                    )
                elif verdict.failure_code == CODE_INCOMPLETE_SUM:
                    note(
                        CODE_INCOMPLETE_SUM,
                        f"incomplete {verdict.document_class} parse of {Path(path).name} "
                        f"from {host} (parse:{parse_strategy}): {verdict.tier.reason}",
                    )
                else:
                    observations.append(f"{host}: scheme not present in {Path(path).name}")
                    note(
                        CODE_MISSING,
                        "scheme not present in any parsed candidate document ("
                        + "; ".join(observations)
                        + ")",
                    )

    if best is not None:
        reason = best[2]
    elif observations:
        reason = "; ".join(observations)
    else:
        reason = "no allow-listed candidate document was obtained via web search"
    return ChannelResult(
        channel=CHANNEL,
        success=False,
        reason=with_untrusted(reason),
        new_document_paths=list(new_paths),
        strategy_used=strategy_used,
        failure_code=best[1] if best else CODE_MISSING,
    )


__all__ = [
    "ADVISORKHOJ_HOST",
    "AMFI_HOST",
    "ALLOWED_SCHEMES",
    "BASE_DIR",
    "Blocked",
    "Candidate",
    "CHANNEL",
    "CODE_INCOMPLETE_SUM",
    "CODE_MISSING",
    "CODE_PARSER_PARTIAL",
    "CODE_SUSPECT_SCALE",
    "CODE_TOPN_ONLY",
    "NullSearchProvider",
    "REGISTRY_PATH",
    "REGISTRY_URL_FIELDS",
    "REASON_SKIPPED_NO_PROVIDER",
    "REASON_SKIPPED_UNTRUSTED_HOST",
    "SearchProvider",
    "WAF_CODE",
    "build_allowlist",
    "host_of",
    "is_allowed",
    "normalize_host",
    "plan",
    "reset_allowlist_cache",
    "run",
]
