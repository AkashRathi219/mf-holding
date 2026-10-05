"""Channel 1 ``amc_recheck`` - alternate AMC-site locations + forced alternate
parse strategy (SPEC §11.3 channel 1, AC-18, T33).

For an escalated ticket ``(amc, scheme, month)`` whose parsed holdings were
incomplete, this channel re-probes ALTERNATE LOCATIONS on that AMC's own
website and re-parses with a parse strategy DIFFERENT from the one that
produced the incomplete set (the forced-alternate rule - the agent must not
repeat the failing parse, PLAN T21/R14).

The five AC-18 location categories, each grounded in a pattern the repo's own
adapters already use (``src/amc_adapters/*.py``, ``docs/data_sources/amc-websites.md``):

1. **alternate menu paths** - the registry's ``scheme_wise`` per-scheme
   downloads section and statutory-disclosure section paths (Jio's
   ``/statutory-disclosure/disclosures/monthly-portfolio-disclosure``), i.e.
   menu routes other than the monthly-portfolio page the first pass used.
2. **the archive page** - the AMC's downloads history (ICICI's
   ``archive.icicipruamc.com``, Bandhan's WordPress disclosure archive
   ``?posts_per_page=2500``, WhiteOak's ``?month=&year=`` query).
3. **monthly vs fortnightly disclosure tabs** - Sundaram's
   ``GetCategory Catid=Monthly|Fortnightly``, JM's fortnightly portfolio
   XLSX, The Wealth Company's ``/portfolio-documents/monthly|fortnightly/``.
4. **prior filename variants** - the same document published under a compact
   ``DDMMYYYY`` as-of date vs a ``Month-YYYY``/``Mon_YYYY`` token (both forms
   occur across the corpus, e.g. ``Portfolio_31082026.xlsx`` vs
   ``Portfolio_Aug-2026.xlsx``).
5. **consolidated zip members** - the AMC's all-schemes ZIP bundle whose
   inner members are routed per-member via ``src/zip_parser.py`` (the
   Bandhan/Quant ``ERR_ARCHIVE_ZIP_SINGLE_XLS`` pattern, NJ's consolidated
   XLS download).

The list is explicit, ordered (cheapest/highest-yield first) and data-driven:
``AMC_LOCATION_OVERRIDES`` lets a per-AMC entry prepend or extend strategies
without touching the default ladder, and ``enumerate_alternate_locations``
always returns ALL FIVE categories (defaults are never dropped).

Parse strategies (``PARSE_STRATEGIES``) are the repo's real parser
capabilities (``docs/data_sources/amc-websites.md`` §5): the three PDF
extractors, the multi-sheet workbook filter, the high-density PDF page
ranking, zip-member routing, the OCR tier and the AI vision tier.
``pick_alternate_strategy(failed)`` never returns ``failed``.

Success requires a FULL-disclosure document (``src.document_class.classify``)
whose holding set reaches Σ >= 95% (``src.agents.tiers.classify`` -> tier T0
``COMPLETE_100``).  A factsheet top-10 does NOT close the ticket - it is an
accepted T1 elsewhere and is only recorded as a fallback observation in the
result ``reason`` with ``success=False``.

Escalation citizenship (OQ-7): the channel is a pure worker - it never
mutates the escalation queue (the caller owns ``record_attempt``), never
touches the DB, walks its attempts strictly sequentially, tries each attempt
at most once (no retry storm), and NEVER evades a WAF: a block code stops the
walk immediately and is reported.  All I/O is dependency-injected
(``downloader``/``parse`` callables plus an opaque ``session``), so tests run
on fakes with no network and no filesystem writes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.agents import taxonomy
from src.agents.channels import (
    ChannelResult,
    EvaluationVerdict,
    evaluate_candidate_payload,
    extract_scheme_holdings,
)
from src.agents.escalation import Ticket

CHANNEL = "amc_recheck"

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
        raise RuntimeError(f"amc_recheck references unknown taxonomy code {_code!r}")

# Outcome codes ranked most-informative first; the walk reports the
# highest-ranked outcome it observed (ties keep the first occurrence).
_FAILURE_PRIORITY: dict[str, int] = {
    CODE_SUSPECT_SCALE: 4,
    CODE_INCOMPLETE_SUM: 3,
    CODE_TOPN_ONLY: 2,
    CODE_PARSER_PARTIAL: 1,
    CODE_MISSING: 0,
}

# ---------------------------------------------------------------------------
# Location strategies (AC-18, five categories)
# ---------------------------------------------------------------------------

CATEGORY_MENU_PATHS = "alternate_menu_paths"
CATEGORY_ARCHIVE = "archive_page"
CATEGORY_DISCLOSURE_TABS = "monthly_fortnightly_tabs"
CATEGORY_FILENAME_VARIANTS = "prior_filename_variants"
CATEGORY_ZIP_MEMBERS = "consolidated_zip_members"

LOCATION_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MENU_PATHS,
    CATEGORY_ARCHIVE,
    CATEGORY_DISCLOSURE_TABS,
    CATEGORY_FILENAME_VARIANTS,
    CATEGORY_ZIP_MEMBERS,
)


@dataclass(frozen=True)
class LocationStrategy:
    """One alternate place to probe on the AMC's own website (AC-18)."""

    key: str
    category: str
    description: str


DEFAULT_LOCATION_STRATEGIES: tuple[LocationStrategy, ...] = (
    LocationStrategy(
        key="menu_scheme_wise_downloads",
        category=CATEGORY_MENU_PATHS,
        description=(
            "registry 'scheme_wise' per-scheme downloads section - a menu path "
            "other than the monthly-portfolio page the first pass used"
        ),
    ),
    LocationStrategy(
        key="menu_statutory_disclosures",
        category=CATEGORY_MENU_PATHS,
        description=(
            "statutory-disclosure section path (e.g. jioblackrockamc.com/"
            "statutory-disclosure/disclosures/monthly-portfolio-disclosure)"
        ),
    ),
    LocationStrategy(
        key="archive_page",
        category=CATEGORY_ARCHIVE,
        description=(
            "AMC archive/downloads history (e.g. archive.icicipruamc.com, "
            "bandhan WP disclosures?posts_per_page=2500, whiteoak ?month=&year=)"
        ),
    ),
    LocationStrategy(
        key="tab_monthly_disclosure",
        category=CATEGORY_DISCLOSURE_TABS,
        description=(
            "Monthly disclosure tab (sundaram GetCategory Catid=Monthly, "
            "wealthcompany /portfolio-documents/monthly/)"
        ),
    ),
    LocationStrategy(
        key="tab_fortnightly_disclosure",
        category=CATEGORY_DISCLOSURE_TABS,
        description=(
            "Fortnightly disclosure tab (sundaram Catid=Fortnightly, JM "
            "fortnightly portfolio XLSX, wealthcompany fortnightly section)"
        ),
    ),
    LocationStrategy(
        key="filename_variant_compact_date",
        category=CATEGORY_FILENAME_VARIANTS,
        description=(
            "prior filename variant: compact DDMMYYYY as-of date "
            "(e.g. Portfolio_31082026.xlsx) instead of Month-YYYY"
        ),
    ),
    LocationStrategy(
        key="filename_variant_month_year",
        category=CATEGORY_FILENAME_VARIANTS,
        description=(
            "prior filename variant: 'Month-YYYY'/'Mon_YYYY' token "
            "(e.g. Portfolio_Aug-2026.xlsx) instead of compact dates"
        ),
    ),
    LocationStrategy(
        key="consolidated_zip_members",
        category=CATEGORY_ZIP_MEMBERS,
        description=(
            "consolidated all-schemes ZIP bundle; route inner members via "
            "src/zip_parser.py (Bandhan/Quant single-XLS zip, NJ consolidated XLS)"
        ),
    ),
)

# Per-AMC extensions: key is a case-insensitive fragment of the AMC name; the
# value lists strategies to probe FIRST for that AMC (defaults are appended
# after them, so all five AC-18 categories always remain in the list).
AMC_LOCATION_OVERRIDES: dict[str, tuple[LocationStrategy, ...]] = {}


def _overrides_for(amc: object) -> tuple[LocationStrategy, ...]:
    name = str(amc or "").casefold()
    for fragment, strategies in AMC_LOCATION_OVERRIDES.items():
        if fragment.casefold() in name:
            return strategies
    return ()


def enumerate_alternate_locations(amc: object) -> tuple[LocationStrategy, ...]:
    """Ordered alternate-location list for ``amc`` (pure, deterministic).

    Per-AMC overrides (when the name matches) come first, then every default
    strategy not already included - so the result always covers all five
    AC-18 categories.
    """
    overrides = _overrides_for(amc)
    if not overrides:
        return DEFAULT_LOCATION_STRATEGIES
    return overrides + tuple(s for s in DEFAULT_LOCATION_STRATEGIES if s not in overrides)


# ---------------------------------------------------------------------------
# Parse strategies (forced-alternate dimension)
# ---------------------------------------------------------------------------

PARSE_STRATEGIES: tuple[str, ...] = (
    "regex_holdings",
    "pdfplumber_table_extraction",
    "one_line_per_holding_layout",
    "sheet_ranking_multi_sheet",
    "page_ranking_high_density",
    "zip_member_routing",
    "ocr_geometry",
    "ai_extract_vision",
)


def pick_alternate_strategy(failed: str | None) -> str:
    """Next parse strategy after ``failed``; NEVER returns ``failed``.

    An unknown/empty ``failed`` (nothing recorded, or a strategy outside this
    channel's list) starts at the first strategy.
    """
    failed_name = str(failed or "").strip()
    if failed_name not in PARSE_STRATEGIES:
        return PARSE_STRATEGIES[0]
    index = PARSE_STRATEGIES.index(failed_name)
    return PARSE_STRATEGIES[(index + 1) % len(PARSE_STRATEGIES)]


def _parse_rotation(failed: str | None) -> tuple[str, ...]:
    """Parse strategies ordered for the walk: alternate-to-``failed`` first,
    then the remaining ones in canonical order, ``failed`` excluded."""
    failed_name = str(failed or "").strip()
    start = pick_alternate_strategy(failed)
    others = [s for s in PARSE_STRATEGIES if s != failed_name and s != start]
    return (start, *others)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    """One planned probe: an alternate location + an intended parse strategy."""

    location: LocationStrategy
    parse_strategy: str
    description: str


def _build_attempts(
    locations: Sequence[LocationStrategy],
    failed_strategy: str | None,
) -> tuple[Attempt, ...]:
    rotation = _parse_rotation(failed_strategy)
    attempts: list[Attempt] = []
    for index, location in enumerate(locations):
        parse_strategy = rotation[index % len(rotation)]
        attempts.append(
            Attempt(
                location=location,
                parse_strategy=parse_strategy,
                description=(
                    f"{location.category}:{location.key} -> parse:{parse_strategy}"
                ),
            )
        )
    return tuple(attempts)


def plan(ticket: Ticket, *, failed_strategy: str | None = None) -> list[Attempt]:
    """Ordered candidate attempts for ``ticket`` (pure planning, no I/O).

    Walks ``enumerate_alternate_locations(ticket.amc)`` and pairs each
    location with the parse rotation that EXCLUDES ``failed_strategy`` (the
    forced-alternate rule: the channel never re-runs the parse strategy that
    produced the incomplete set).  Deterministic: same inputs -> same order.
    """
    return list(_build_attempts(enumerate_alternate_locations(ticket.amc), failed_strategy))


# ---------------------------------------------------------------------------
# Execution (all I/O injected)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Blocked:
    """Sentinel a downloader returns when the host WAF-blocked the request.

    ``code`` should be a taxonomy code (normally ``ERR_WAF_CLOUDFLARE_1015``);
    a non-taxonomy string is normalised to the canonical WAF code rather than
    invented.
    """

    code: str = WAF_CODE


def _candidate_paths(fetched: object) -> list[str]:
    """Normalise a downloader's return value to an ordered list of paths."""
    if fetched is None:
        return []
    if isinstance(fetched, str):
        return [fetched.strip()] if fetched.strip() else []
    if isinstance(fetched, (list, tuple)):
        return [str(p).strip() for p in fetched if str(p).strip()]
    if isinstance(fetched, (set, frozenset)):
        return sorted(str(p).strip() for p in fetched if str(p).strip())
    return []


# Aliased to the shared implementation in src.agents.channels
_extract_scheme_holdings = extract_scheme_holdings


def _waf_result(
    attempt: Attempt,
    new_paths: list[str],
    code: str,
    *,
    via: str,
) -> ChannelResult:
    return ChannelResult(
        channel=CHANNEL,
        success=False,
        reason=(
            f"WAF block ({code}) reported via {via} at '{attempt.location.key}'; "
            f"stopped without evading, rotating IPs or retrying"
        ),
        new_document_paths=list(new_paths),
        strategy_used=attempt.parse_strategy,
        failure_code=code,
    )


def run(
    ticket: Ticket,
    *,
    session: object = None,
    locations: Sequence[LocationStrategy] | None = None,
    downloader: object = None,
    parse: object = None,
    failed_strategy: str | None = None,
    now: object = None,
) -> ChannelResult:
    """Walk the planned attempts and report one :class:`ChannelResult`.

    Injected dependencies (no real network, no filesystem access of its own):

    * ``downloader(attempt, ticket, session)`` -> ordered sequence of candidate
      document paths for the attempt's location strategy, a :class:`Blocked`
      sentinel when the host WAF-blocked, or ``[]`` when nothing was found.
    * ``parse(document_path, parse_strategy, session)`` -> parsed payload dict
      in one of the repo's parsed shapes (see
      :func:`_extract_scheme_holdings`); raises on parse failure.
    * ``session`` is opaque context (e.g. an httpx client) passed through.
    * ``locations`` overrides the default alternate-location list.
    * ``now`` is accepted for caller uniformity (episode stamping); the
      channel itself is clock-free.

    The walk is strictly sequential (OQ-7), tries each attempt at most once,
    stops immediately on a WAF block (no evasion, no retry storm), and never
    mutates the escalation queue or the DB.  Success requires a
    full-disclosure document (``document_class.classify``) reaching Σ >= 95
    (``tiers.classify`` -> ``COMPLETE_100``); a factsheet top-10 is recorded
    as a fallback observation with ``success=False``.
    """
    if downloader is None or parse is None:
        raise ValueError("amc_recheck.run requires injected downloader and parse callables")

    if locations is None:
        locations = enumerate_alternate_locations(ticket.amc)
    attempts = _build_attempts(list(locations), failed_strategy)

    new_paths: list[str] = []
    seen_paths: set[str] = set()
    observations: list[str] = []
    best: tuple[int, str, str] | None = None

    def note(code: str, reason: str) -> None:
        nonlocal best
        priority = _FAILURE_PRIORITY.get(code, -1)
        if best is None or priority > best[0]:
            best = (priority, code, reason)

    for attempt in attempts:
        location = attempt.location
        try:
            fetched = downloader(attempt, ticket, session)
        except Exception as exc:
            code = taxonomy.classify_exception(exc)
            if code == WAF_CODE:
                return _waf_result(attempt, new_paths, code, via="downloader")
            code = code or CODE_MISSING
            reason = f"download failed at '{location.key}' ({code}); document not obtained"
            observations.append(f"{location.key}: download failed ({code})")
            note(code, reason)
            continue
        if isinstance(fetched, Blocked):
            code = fetched.code if taxonomy.is_valid_code(fetched.code) else WAF_CODE
            return _waf_result(attempt, new_paths, code, via="downloader sentinel")
        paths = _candidate_paths(fetched)
        if not paths:
            observations.append(f"{location.key}: no candidate documents")
            note(
                CODE_MISSING,
                "no candidate documents at any alternate location ("
                + "; ".join(observations)
                + ")",
            )
            continue

        for path in paths:
            if path not in seen_paths:
                seen_paths.add(path)
                new_paths.append(path)
            try:
                payload = parse(path, attempt.parse_strategy, session)
            except Exception as exc:
                code = taxonomy.classify_exception(exc)
                if code == WAF_CODE:
                    return _waf_result(attempt, new_paths, code, via="parser")
                code = code or CODE_PARSER_PARTIAL
                note(
                    code,
                    f"parse failed at '{location.key}' on {Path(path).name} "
                    f"(parse:{attempt.parse_strategy}): {code}",
                )
                continue
            if not isinstance(payload, Mapping) or not payload:
                note(
                    CODE_PARSER_PARTIAL,
                    f"empty parse at '{location.key}' on {Path(path).name} "
                    f"(parse:{attempt.parse_strategy})",
                )
                continue

            verdict = evaluate_candidate_payload(payload, ticket.scheme, source_file=str(path))

            if verdict.is_t0:
                return ChannelResult(
                    channel=CHANNEL,
                    success=True,
                    reason=(
                        f"closed by {verdict.document_class} document {Path(path).name} at "
                        f"'{location.key}' (parse:{attempt.parse_strategy}): {verdict.tier.reason}"
                    ),
                    new_document_paths=list(new_paths),
                    strategy_used=attempt.parse_strategy,
                    failure_code=None,
                )
            if verdict.failure_code == CODE_SUSPECT_SCALE:
                note(
                    CODE_SUSPECT_SCALE,
                    f"suspect scale at '{location.key}' on {Path(path).name} "
                    f"(parse:{attempt.parse_strategy}): {verdict.tier.reason}",
                )
            elif verdict.failure_code == CODE_TOPN_ONLY:
                note(
                    CODE_TOPN_ONLY,
                    f"only a factsheet_topn document ({Path(path).name}, "
                    f"Σ={verdict.tier.coverage_pct:.2f}%) at '{location.key}' "
                    f"(parse:{attempt.parse_strategy}) - accepted T1 elsewhere, "
                    f"does NOT close this ticket",
                )
            elif verdict.failure_code == CODE_INCOMPLETE_SUM:
                note(
                    CODE_INCOMPLETE_SUM,
                    f"incomplete {verdict.document_class} parse at '{location.key}' on "
                    f"{Path(path).name} (parse:{attempt.parse_strategy}): {verdict.tier.reason}",
                )
            else:
                observations.append(f"{location.key}: scheme not present in {Path(path).name}")
                note(
                    CODE_MISSING,
                    "scheme not present in any parsed candidate document ("
                    + "; ".join(observations)
                    + ")",
                )

    if best is not None:
        _, code, reason = best
        return ChannelResult(
            channel=CHANNEL,
            success=False,
            reason=reason,
            new_document_paths=list(new_paths),
            strategy_used=attempts[-1].parse_strategy if attempts else "",
            failure_code=code,
        )
    return ChannelResult(
        channel=CHANNEL,
        success=False,
        reason="no alternate location yielded a candidate document",
        new_document_paths=list(new_paths),
        strategy_used=attempts[-1].parse_strategy if attempts else "",
        failure_code=CODE_MISSING,
    )


__all__ = [
    "AMC_LOCATION_OVERRIDES",
    "Attempt",
    "Blocked",
    "CATEGORY_ARCHIVE",
    "CATEGORY_DISCLOSURE_TABS",
    "CATEGORY_FILENAME_VARIANTS",
    "CATEGORY_MENU_PATHS",
    "CATEGORY_ZIP_MEMBERS",
    "CHANNEL",
    "DEFAULT_LOCATION_STRATEGIES",
    "LocationStrategy",
    "LOCATION_CATEGORIES",
    "PARSE_STRATEGIES",
    "enumerate_alternate_locations",
    "pick_alternate_strategy",
    "plan",
    "run",
]
