"""Tier lookup / UI surfacing contract for the integrity tier sidecar (SPEC §11.0 / T36 / AC-21).

The integrity auditor (``src.agents.integrity``, T20) writes the AC-21 sidecar
``data/reference/integrity_tiers.json``: an OBJECT keyed by the scheme-month
string ``"<amc>|<fund_name>|<YYYY-MM>"`` — the literal bucket ``"unknown"``
for schemes whose ``schemes.as_of`` is empty or not a clean date — carrying
one ``{tier, tier_code, document_class, coverage_pct, n_holdings, escalate,
flags, reason, source, as_of}`` record per audited scheme.  This module is
the read-only, query-time join the UI uses: it NEVER opens or alters the
webapp SQLite DB (SPEC §2 prohibition — the tier is joined from the
``data/reference/`` sidecar at query time) and never writes the sidecar.

Fail-open rule (the point of AC-21): an absent key, a missing/malformed
sidecar, or a malformed record resolves to ``None`` / ``"Unknown"`` — NEVER
to a complete-looking result.  A scheme whose completeness cannot be proven
from the sidecar is displayed as unknown/pending, never silently as full
holdings.  ``display_label`` maps the §11.0 tiers to the UI strings (T0
``COMPLETE_100`` -> "Full portfolio", T1 ``TOP10_FALLBACK`` -> "Top 10 only",
T2 -> "Pending — hunting for full portfolio", T3 -> "Pending manual") and
``is_full_portfolio`` is the single template gate that is True for a proven
T0 record only.

Import-light by design (``json`` + ``pathlib`` only; no network, no DB
driver, no agent-package imports) so the webapp request path can import it
freely.  The tier codes/names restated below mirror ``src.agents.tiers``
(T20) but are kept local so this module stays standalone.  Callers rendering
many rows should call :func:`load_tiers` once and index the returned dict
directly with :func:`tier_key` — it IS the lookup table, keyed exactly as
the writer keyed it.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_REFERENCE = Path("data") / "reference"
DEFAULT_TIERS_PATH = DATA_REFERENCE / "integrity_tiers.json"

UNKNOWN_MONTH = "unknown"

TIER_CODE_COMPLETE_100 = "T0"
TIER_CODE_TOP10_FALLBACK = "T1"
TIER_CODE_ESCALATE = "T2"
TIER_CODE_MANUAL = "T3"

LABEL_FULL_PORTFOLIO = "Full portfolio"
LABEL_TOP10_ONLY = "Top 10 only"
LABEL_PENDING_HUNT = "Pending — hunting for full portfolio"
LABEL_PENDING_MANUAL = "Pending manual"
LABEL_UNKNOWN = "Unknown"

DISPLAY_LABELS: dict[str, str] = {
    TIER_CODE_COMPLETE_100: LABEL_FULL_PORTFOLIO,
    TIER_CODE_TOP10_FALLBACK: LABEL_TOP10_ONLY,
    TIER_CODE_ESCALATE: LABEL_PENDING_HUNT,
    TIER_CODE_MANUAL: LABEL_PENDING_MANUAL,
    "COMPLETE_100": LABEL_FULL_PORTFOLIO,
    "TOP10_FALLBACK": LABEL_TOP10_ONLY,
    "ESCALATE": LABEL_PENDING_HUNT,
    "MANUAL": LABEL_PENDING_MANUAL,
}


def _month_bucket(as_of: object) -> str:
    """``YYYY-MM`` bucket for a ``schemes.as_of`` value (mirrors the writer).

    ``as_of[:7]`` when the value starts with a clean ``YYYY-MM`` date,
    otherwise the literal ``"unknown"`` bucket — the same decision
    ``integrity._scheme_month`` made when it wrote the key, so non-date
    ``as_of`` values join the ``|unknown`` entries the auditor created for
    them instead of missing into ``None``.
    """
    s = str(as_of or "").strip()
    if len(s) >= 7 and s[4] == "-" and s[:4].isdigit() and s[5:7].isdigit():
        return s[:7]
    return UNKNOWN_MONTH


def tier_key(amc: object, fund_name: object, as_of: object) -> str:
    """Sidecar key ``"<amc>|<fund_name>|<month>"`` in the writer's exact format.

    Components are used verbatim (no normalisation) so the key matches the
    sidecar byte-for-byte; only ``as_of`` is bucketed (see ``_month_bucket``).
    """
    return f"{amc or ''}|{fund_name or ''}|{_month_bucket(as_of)}"


def load_tiers(path: str | Path = DEFAULT_TIERS_PATH) -> dict[str, dict]:
    """Load the tier sidecar tolerantly; never raise (missing file -> ``{}``).

    A missing/unreadable file, malformed JSON, or a non-object document
    yields ``{}``; entries whose key is not a string or whose value is not
    an object are skipped as malformed.  Everything else is returned exactly
    as the auditor stored it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {
        key: value
        for key, value in doc.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def lookup(
    amc: object,
    fund_name: object,
    as_of: object,
    *,
    path: str | Path = DEFAULT_TIERS_PATH,
) -> dict | None:
    """Stored tier record for the scheme-month, or ``None`` when unknown.

    Builds the writer's key via :func:`tier_key` and returns the sidecar
    record as-is.  ``None`` means "no audited tier for this key" — callers
    must treat it as Unknown/Pending, never as complete (fail open to
    unknown, never to complete; see :func:`display_label`).
    """
    return load_tiers(path).get(tier_key(amc, fund_name, as_of))


def display_label(record: object) -> str:
    """UI label for a stored tier record (SPEC §11.3 display contract).

    T0 ``COMPLETE_100`` -> "Full portfolio", T1 ``TOP10_FALLBACK`` -> "Top 10
    only", T2 -> "Pending — hunting for full portfolio", T3 -> "Pending
    manual".  ``None``, a malformed record, or an unrecognised tier resolves
    to ``"Unknown"`` — never to a complete-looking label.
    """
    if not isinstance(record, dict):
        return LABEL_UNKNOWN
    code = record.get("tier_code") or record.get("tier")
    if not isinstance(code, str):
        return LABEL_UNKNOWN
    return DISPLAY_LABELS.get(code, LABEL_UNKNOWN)


def is_full_portfolio(record: object) -> bool:
    """True only for a proven T0 ``COMPLETE_100`` record; False otherwise.

    The single gate templates should consult before rendering full holdings:
    ``None``, T1 top-10, T2/T3 pending and malformed records are all False,
    so partial data can never be presented as complete (AC-21).
    """
    if not isinstance(record, dict):
        return False
    code = record.get("tier_code") or record.get("tier")
    return code in (TIER_CODE_COMPLETE_100, "COMPLETE_100")
