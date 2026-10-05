"""Pure completeness-ladder tiering for parsed holding sets (SPEC §11.0 / T32).

Every scheme-month is placed in exactly one tier of the §11.0 ladder.  The
judgement is CLASS-RELATIVE: a ``factsheet_topn`` document publishes only its
top ~10 holdings (sums ~35-60%), so a 10-row factsheet summing ~45% is
complete FOR ITS DOCUMENT CLASS (tier T1 ``TOP10_FALLBACK`` - accepted, never
escalated), while a full-disclosure-class document that fails to reach 95% is
a real failure (tier T2 ``ESCALATE``).  Judging completeness without the
``document_class`` would re-create the false-positive escalation storm §11.0
exists to prevent.

Precedence (evaluated in this order; ``classify`` is PURE and deterministic -
no file, network or clock access, same input -> same output):

    a. no rows at all -> tier T2 ``ESCALATE`` + flag ``MISSING``,
       escalate=True.
    b. coverage > 105 or any single weight > 100 -> flag ``SUSPECT_SCALE`` +
       escalate=True.  Orthogonal: the flag sits ON TOP of the tier chosen
       below (a T0 whose coverage is in band but carries a 140% weight, or a
       top-10-shaped factsheet summing > 105 with every weight <= 100, keeps
       its T0/T1 label but IS escalated - a scale/parse bug is always
       escalated per §11.1).
    c. ``factsheet_topn`` with ~top-N rows (8-12) and every weight <= 100 ->
       tier T1 ``TOP10_FALLBACK``, escalate=False (unless b fired).
    d. ``full_portfolio`` / ``fortnightly`` / ``unknown`` with
       95 <= coverage <= 105 -> tier T0 ``COMPLETE_100``, escalate=False
       (unless b fired).
    e. anything else -> tier T2 ``ESCALATE``, escalate=True.

Key safety property: ``unknown`` - or a missing/unrecognised
``document_class``, which is normalised to ``unknown`` - is judged by the
STRICT full-disclosure rule (d/e) and must NEVER return T1, so a missing
field can never silently become an accepted top-10.

Weights are read from ``percent_nav`` / ``weight_pct`` / ``pct`` / ``%`` (in
that order) and may be floats or ``"2.35%"``-style strings; rows without a
parseable weight are skipped in the coverage sum but still counted
(``n_holdings`` and the ``reason`` report them).  A top-N verdict requires at
least one parseable weight: a table whose weights are entirely unparseable
cannot be verified and falls through to T2.

T3 ``MANUAL`` is never returned here: it is assigned by the escalation ladder
(§11.3) after all channels are exhausted, not by this classifier.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.document_class import (
    DOCUMENT_CLASSES,
    FACTSHEET_TOPN,
    FORTNIGHTLY,
    FULL_PORTFOLIO,
    UNKNOWN,
)

COMPLETE_MIN = 95.0
COMPLETE_MAX = 105.0
TOPN_EXPECTED_ROWS = 10
TOPN_MIN_ROWS = 8
TOPN_MAX_ROWS = 12

FLAG_MISSING = "MISSING"
FLAG_SUSPECT_SCALE = "SUSPECT_SCALE"

TIER_COMPLETE_100 = "COMPLETE_100"
TIER_TOP10_FALLBACK = "TOP10_FALLBACK"
TIER_ESCALATE = "ESCALATE"
TIER_MANUAL = "MANUAL"

TIER_CODES: dict[str, str] = {
    TIER_COMPLETE_100: "T0",
    TIER_TOP10_FALLBACK: "T1",
    TIER_ESCALATE: "T2",
    TIER_MANUAL: "T3",
}

_WEIGHT_KEYS = ("percent_nav", "weight_pct", "pct", "%")
_STRICT_CLASSES = (FULL_PORTFOLIO, FORTNIGHTLY, UNKNOWN)


@dataclass(frozen=True)
class TierResult:
    """One scheme-month's place on the §11.0 ladder (immutable, JSON-ready)."""

    tier: str
    tier_code: str
    coverage_pct: float
    n_holdings: int
    document_class: str
    escalate: bool
    flags: tuple[str, ...]
    reason: str


def _parse_percent(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _row_weight(row: object) -> float | None:
    if not isinstance(row, Mapping):
        return None
    for key in _WEIGHT_KEYS:
        if key not in row:
            continue
        parsed = _parse_percent(row[key])
        if parsed is not None:
            return parsed
    return None


def _escalate_reason(
    document_class: str,
    n_holdings: int,
    coverage: float,
    max_weight: float | None,
    has_weights: bool,
) -> str:
    if document_class == FACTSHEET_TOPN:
        if not has_weights:
            return f"factsheet_topn: no parseable weight in any of {n_holdings} row(s)"
        if not TOPN_MIN_ROWS <= n_holdings <= TOPN_MAX_ROWS:
            return (
                f"factsheet_topn: {n_holdings} rows outside top-N band "
                f"{TOPN_MIN_ROWS}-{TOPN_MAX_ROWS}, coverage {coverage:.2f}%"
            )
        return (
            f"factsheet_topn: single weight {max_weight:.2f} exceeds 100, "
            f"coverage {coverage:.2f}%"
        )
    if coverage > COMPLETE_MAX:
        return (
            f"strict full-disclosure rule for '{document_class}': "
            f"coverage {coverage:.2f}% above {COMPLETE_MAX}"
        )
    return (
        f"strict full-disclosure rule for '{document_class}': "
        f"coverage {coverage:.2f}% below {COMPLETE_MIN}"
    )


def classify(
    holdings: Sequence[Mapping[str, object]] | None,
    document_class: str | None,
) -> TierResult:
    """Place a holding set on the §11.0 ladder (pure, deterministic).

    ``holdings`` is a list of row mappings (or ``None``/``[]`` when the
    scheme-month is missing entirely); ``document_class`` is one of the four
    ``src.document_class`` constants - anything else (including ``None``) is
    normalised to ``unknown`` and judged by the strict full-disclosure rule.
    """
    dc = document_class if document_class in DOCUMENT_CLASSES else UNKNOWN
    rows = list(holdings) if holdings else []

    if not rows:
        return TierResult(
            tier=TIER_ESCALATE,
            tier_code=TIER_CODES[TIER_ESCALATE],
            coverage_pct=0.0,
            n_holdings=0,
            document_class=dc,
            escalate=True,
            flags=(FLAG_MISSING,),
            reason="no holding rows present",
        )

    weights: list[float] = []
    skipped = 0
    for row in rows:
        weight = _row_weight(row)
        if weight is None:
            skipped += 1
        else:
            weights.append(weight)

    coverage = round(sum(weights), 6) if weights else 0.0
    n_holdings = len(rows)
    max_weight = max(weights) if weights else None
    suspect = coverage > COMPLETE_MAX or (max_weight is not None and max_weight > 100.0)

    flags: list[str] = []
    escalate = False
    if suspect:
        flags.append(FLAG_SUSPECT_SCALE)
        escalate = True
    skipped_note = f"; {skipped} unparseable row(s) skipped" if skipped else ""

    if (
        dc == FACTSHEET_TOPN
        and weights
        and TOPN_MIN_ROWS <= n_holdings <= TOPN_MAX_ROWS
        and max_weight is not None
        and max_weight <= 100.0
    ):
        tier = TIER_TOP10_FALLBACK
        reason = (
            f"factsheet_topn: {n_holdings} rows in top-N band "
            f"{TOPN_MIN_ROWS}-{TOPN_MAX_ROWS} (~{TOPN_EXPECTED_ROWS}), "
            f"coverage {coverage:.2f}%"
        ) + skipped_note
    elif dc in _STRICT_CLASSES and COMPLETE_MIN <= coverage <= COMPLETE_MAX:
        tier = TIER_COMPLETE_100
        reason = (
            f"full-disclosure class '{dc}': coverage {coverage:.2f}% "
            f"within [{COMPLETE_MIN}, {COMPLETE_MAX}]"
        ) + skipped_note
    else:
        tier = TIER_ESCALATE
        escalate = True
        reason = _escalate_reason(dc, n_holdings, coverage, max_weight, bool(weights)) + skipped_note

    if suspect:
        reason += (
            "; SUSPECT_SCALE: coverage > 105 or a single weight > 100 "
            "(scale/parse bug, always escalated)"
        )

    return TierResult(
        tier=tier,
        tier_code=TIER_CODES[tier],
        coverage_pct=coverage,
        n_holdings=n_holdings,
        document_class=dc,
        escalate=escalate,
        flags=tuple(flags),
        reason=reason,
    )
