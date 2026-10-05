"""Document-class classification for parsed holding sets (SPEC §11.0 / OQ-8).

Every parsed holding set carries a ``document_class`` so the integrity auditor
can judge completeness RELATIVE TO the document class: a factsheet publishes
only the top 10 (sums ~35-60%) and is therefore accepted (tier T1
``TOP10_FALLBACK``), while a full monthly portfolio disclosure must reach 95%
or it is a real failure (T2).  ``unknown`` is the SAFE DEFAULT and is judged by
the strict (full-disclosure) rule, so a missing field can never silently become
an accepted T1.

Classes (``DOCUMENT_CLASSES``):
    ``full_portfolio``  the complete monthly portfolio disclosure (all holdings)
    ``fortnightly``     the mid-month fortnightly disclosure
    ``factsheet_topn``  a factsheet / grouped factsheet (inherently top-N, ~10 rows)
    ``unknown``         cannot be determined -> strict full-disclosure rule

Precedence (most specific first; first match wins):
    1. ``factsheet_topn``  - a ``factsheet``/``fact sheet``/``fact_sheet`` token
       (incl. ``grouped_factsheet``) in the source path/filename, a truthy
       ``metadata.grouped_factsheet`` flag, or a scheme/dict name carrying an
       explicit top-N holdings marker ("Top 10 Holdings", "Top Ten Holding",
       "Top Holdings", "Major Holdings").
    2. ``fortnightly``     - a ``fortnight`` token in the name/URL/metadata or
       scheme names.
    3. ``full_portfolio``  - a ``portfolio`` token (covers "monthly portfolio",
       "monthly_portfolio", "portfolio disclosure", "portfolio statement").
    4. ``unknown``         - no signal.

Factsheet/fortnightly outrank the generic ``portfolio`` token because they are
the more specific document types; a top-N factsheet misjudged as a full
disclosure would cause the false-positive escalation storm §11.0 exists to
prevent.  A bare "top 10" inside a scheme name is deliberately NOT a marker:
real fund brands ("Motilal Oswal BSE Top 10 Banks ETF", "Kotak Nifty Top 10
Equal Weight Index Fund") sit inside full monthly portfolio disclosures
(verified against ``data/parsed``), so the marker must read like a holdings
table header ("... top 10 holding(s)") to count.

The function is PURE and deterministic: same input -> same output, no I/O, no
clock.  ``file_type`` (excel/pdf/zip/html/...) is accepted for call-site
uniformity but deliberately unused - the storage format is orthogonal to the
document class, as is the row count (a 10-row full disclosure of a micro fund
must not be misread as top-N; the tiering layer owns row-count rules).
"""

from __future__ import annotations

import re

FULL_PORTFOLIO = "full_portfolio"
FORTNIGHTLY = "fortnightly"
FACTSHEET_TOPN = "factsheet_topn"
UNKNOWN = "unknown"

DOCUMENT_CLASSES: tuple[str, ...] = (
    FULL_PORTFOLIO,
    FORTNIGHTLY,
    FACTSHEET_TOPN,
    UNKNOWN,
)

_FACTSHEET_RE = re.compile(r"fact[-_ ]?sheet", re.IGNORECASE)
_TOPN_MARKER_RE = re.compile(
    r"top[-_ ]*(?:10|ten)[-_ ]*holdings?\b"
    r"|top[-_ ]*holdings\b"
    r"|major[-_ ]*holdings?\b",
    re.IGNORECASE,
)
_FORTNIGHT_RE = re.compile(r"fortnight", re.IGNORECASE)
_PORTFOLIO_RE = re.compile(r"portfolio", re.IGNORECASE)

_SCHEME_NAME_KEYS = ("scheme_name", "fund_name")


def _name_texts(payload: dict) -> list[str]:
    """Scheme/dict names carried by the payload (dict- and list-shaped)."""
    parts: list[str] = []
    schemes = payload.get("schemes")
    if isinstance(schemes, dict):
        for key, scheme in schemes.items():
            parts.append(str(key))
            if isinstance(scheme, dict):
                for k in _SCHEME_NAME_KEYS:
                    v = scheme.get(k)
                    if isinstance(v, str) and v:
                        parts.append(v)
    elif isinstance(schemes, list):
        for scheme in schemes:
            if isinstance(scheme, dict):
                v = scheme.get("name")
                if isinstance(v, str) and v:
                    parts.append(v)
    return parts


def _signal_texts(payload: dict, source_file: str) -> list[str]:
    """Every text the classifier may look at (path, metadata, scheme names)."""
    parts: list[str] = []
    if source_file:
        parts.append(source_file)
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        if meta.get("grouped_factsheet"):
            parts.append("grouped_factsheet")
        for v in meta.values():
            if isinstance(v, str) and v:
                parts.append(v)
    parts.extend(_name_texts(payload))
    return parts


def classify(payload: dict, *, source_file: str = "", file_type: str = "") -> str:
    """Return the document class of a parsed holding set (pure, deterministic).

    ``source_file``/``file_type`` keyword arguments override the payload's own
    ``source_file``/``file``/``file_type`` entries when provided; the AI
    sidecar shape (``{"file": ..., "schemes": [{"name": ...}]}``) is accepted
    alongside the parser shape (``{"source_file": ..., "schemes": {...}}``).
    """
    payload = payload if isinstance(payload, dict) else {}
    sf = source_file or payload.get("source_file") or payload.get("file") or ""
    parts = _signal_texts(payload, str(sf))
    if any(_FACTSHEET_RE.search(p) for p in parts):
        return FACTSHEET_TOPN
    if any(_TOPN_MARKER_RE.search(p) for p in parts):
        return FACTSHEET_TOPN
    if any(_FORTNIGHT_RE.search(p) for p in parts):
        return FORTNIGHTLY
    if any(_PORTFOLIO_RE.search(p) for p in parts):
        return FULL_PORTFOLIO
    return UNKNOWN
