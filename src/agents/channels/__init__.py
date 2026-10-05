"""Escalation channel package (SPEC §11.3, T33).

Each escalation channel is one independent, skippable strategy the ladder
walks in the fixed §11.3 order (``amc_recheck`` -> ``web_search`` -> ``amfi``
-> ``advisorkhoj`` -> ``manual``).  To stay uniform and swappable every
channel module exposes:

    CHANNEL  -- its name, one of :data:`src.agents.escalation.CHANNELS`
    run(ticket, **injected) -> ChannelResult

The shared contract every channel honours:

* All I/O is dependency-injected (downloader/parse/provider callables and an
  opaque session); a channel performs no network or filesystem access of its
  own, so tests run on fakes only.
* A channel NEVER mutates the escalation queue, the manual register or the
  webapp DB - the caller owns ``escalation.EscalationQueue.record_attempt``
  and episode logging; channels only report.
* Every FAILED result carries a ``failure_code`` from the fixed 15-code
  taxonomy (``src.agents.taxonomy``); a SUCCESS result carries ``None``.
  Channels never invent codes and never evade a WAF block (a block stops the
  channel immediately and is reported).
* Closing a ticket means a FULL-disclosure document reaching Σ >= 95%
  (tier T0 ``COMPLETE_100``); a factsheet top-10 is an accepted T1 fallback
  and never closes a T2 ticket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents import tiers
from src.agents.escalation import CHANNELS
from src.agents.tiers import TierResult
from src.document_class import FACTSHEET_TOPN, classify as classify_document

CHANNEL_ORDER: tuple[str, ...] = tuple(CHANNELS)


@dataclass
class ChannelResult:
    """Uniform outcome record every escalation channel returns (§11.3).

    ``channel`` is the §11.3 channel name; ``success`` is True only when the
    channel obtained a full-disclosure document that reaches Σ >= 95% (T0).
    ``reason`` is the human-readable evidence the caller logs into the
    episode (and, on failure, may quote in the ticket).  ``new_document_paths``
    lists documents the channel newly obtained (empty when it fetched
    nothing) so the caller can ingest them even on a failed run.
    ``strategy_used`` is the strategy of the attempt that decided the outcome
    (the closing parse strategy on success; the last attempted one on
    failure).  ``failure_code`` is ``None`` on success and exactly one of the
    15 fixed taxonomy codes on failure (AC-5) - never invented, never left
    ``None`` on a failed run.
    """

    channel: str
    success: bool
    reason: str
    new_document_paths: list[str] = field(default_factory=list)
    strategy_used: str = ""
    failure_code: str | None = None


@dataclass(frozen=True)
class EvaluationVerdict:
    """Outcome of evaluating one parsed candidate document against a scheme."""

    is_t0: bool
    document_class: str
    rows: list[Mapping]
    tier: TierResult
    failure_code: str | None
    scheme_found: bool


def _norm_name(name: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())


def _name_matches(candidate: object, scheme: str) -> bool:
    left, right = _norm_name(candidate), _norm_name(scheme)
    return bool(left) and bool(right) and (left == right or left in right or right in left)


def _holdings_of(value: object) -> list[Mapping]:
    if not isinstance(value, dict):
        return []
    holdings = value.get("holdings")
    if isinstance(holdings, list) and holdings:
        return [row for row in holdings if isinstance(row, Mapping)]
    rows: list[Mapping] = []
    plans = value.get("plans")
    if isinstance(plans, dict):
        for plan in plans.values():
            if isinstance(plan, dict):
                plan_rows = plan.get("holdings")
                if isinstance(plan_rows, list):
                    rows.extend(row for row in plan_rows if isinstance(row, Mapping))
    return rows


def extract_scheme_holdings(payload: Mapping, scheme: str) -> list[Mapping]:
    """Holding rows for ``scheme`` from a parsed payload (real repo shapes).

    Supported shapes: ``{"schemes": {fund: {"holdings": [...]}}}`` (amc_websites
    / amfi), ``{"schemes": [{"name": ..., "holdings": [...]}]}``, the
    Advisorkhoj shape ``{"files": [{"sheets": [{"scheme": ..., "plans": {...}}]}]}``,
    and the flat PDF buckets (``equity_holdings``/``debt_holdings``, falling
    back to ``holdings`` then ``top_holdings``).
    """
    rows: list[Mapping] = []

    schemes = payload.get("schemes")
    if isinstance(schemes, dict):
        for key, value in schemes.items():
            if _name_matches(key, scheme):
                rows.extend(_holdings_of(value))
    elif isinstance(schemes, list):
        for value in schemes:
            if not isinstance(value, dict):
                continue
            if (
                _name_matches(value.get("name"), scheme)
                or _name_matches(value.get("scheme_name"), scheme)
                or _name_matches(value.get("fund_name"), scheme)
            ):
                rows.extend(_holdings_of(value))

    if not rows:
        for files in (payload.get("files") or []):
            if not isinstance(files, dict):
                continue
            for sheet in files.get("sheets") or []:
                if isinstance(sheet, dict) and _name_matches(sheet.get("scheme"), scheme):
                    rows.extend(_holdings_of(sheet))

    if not rows:
        equity = payload.get("equity_holdings")
        debt = payload.get("debt_holdings")
        if isinstance(equity, list) or isinstance(debt, list):
            for bucket in (equity, debt):
                if isinstance(bucket, list):
                    rows.extend(row for row in bucket if isinstance(row, Mapping))
        else:
            flat = payload.get("holdings")
            if not isinstance(flat, list):
                flat = payload.get("top_holdings")
            if isinstance(flat, list):
                rows.extend(row for row in flat if isinstance(row, Mapping))

    return rows


def evaluate_candidate_payload(
    payload: Mapping,
    scheme: str,
    *,
    source_file: str = "",
) -> EvaluationVerdict:
    """Evaluate a parsed payload for ``scheme`` against tier T0 (AC-16).

    Classifies the document class, extracts the scheme holdings, and runs
    tier classification via ``src.agents.tiers.classify``. Returns an
    :class:`EvaluationVerdict` indicating whether tier T0 COMPLETE_100 was
    attained (excluding factsheet_topn documents), or which failure code
    (ERR_SCALE_SUSPECT_WEIGHTS, ERR_HOLDINGS_TOPN_ONLY,
    ERR_HOLDINGS_INCOMPLETE_SUM, ERR_SCHEME_MISSING_IN_DB) applies.
    """
    document_class = classify_document(payload, source_file=source_file)
    rows = extract_scheme_holdings(payload, scheme)
    tier = tiers.classify(rows, document_class)
    is_t0 = tier.tier == tiers.TIER_COMPLETE_100 and document_class != FACTSHEET_TOPN
    if is_t0:
        return EvaluationVerdict(
            is_t0=True,
            document_class=document_class,
            rows=rows,
            tier=tier,
            failure_code=None,
            scheme_found=bool(rows),
        )
    if tiers.FLAG_SUSPECT_SCALE in tier.flags:
        code = "ERR_SCALE_SUSPECT_WEIGHTS"
    elif tier.tier == tiers.TIER_TOP10_FALLBACK or document_class == FACTSHEET_TOPN:
        code = "ERR_HOLDINGS_TOPN_ONLY"
    elif rows:
        code = "ERR_HOLDINGS_INCOMPLETE_SUM"
    else:
        code = "ERR_SCHEME_MISSING_IN_DB"
    return EvaluationVerdict(
        is_t0=False,
        document_class=document_class,
        rows=rows,
        tier=tier,
        failure_code=code,
        scheme_found=bool(rows),
    )


__all__ = [
    "CHANNEL_ORDER",
    "ChannelResult",
    "EvaluationVerdict",
    "evaluate_candidate_payload",
    "extract_scheme_holdings",
]
