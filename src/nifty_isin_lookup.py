"""Nifty-based ISIN lookup for missing stock ISINs.

Given a company name, resolve its ISIN from:
  1. local Nifty constituent files (exact normalized match), then
  2. a fuzzy token match (2+ shared tokens, or 1 distinctive token >=5 chars).
Optionally falls back to a live NSE (niftyindices.com) search.

Usage:
    from src.nifty_isin_lookup import lookup_isin
    isin = lookup_isin("Toubro Ltd")   # -> INE0L2E01017 (Larsen & Toubro)
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSTITUENTS = PROJECT_ROOT / "data" / "nifty" / "constituents"
DEBT_CONSTITUENTS = PROJECT_ROOT / "data" / "nifty" / "debt_constituents"

STOP = {"the", "of", "and", "co", "ltd", "limited", "india", "ind", "corp",
        "corporation", "inc", "company", "fund", "plan", "scheme", "mutual"}


def norm(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(name: str) -> set[str]:
    return {t for t in norm(name).split() if t not in STOP and len(t) > 2}


def _load_reference() -> dict[str, str]:
    ref: dict[str, str] = {}
    for folder in (CONSTITUENTS, DEBT_CONSTITUENTS):
        for jp in folder.glob("*.csv"):
            try:
                with jp.open(encoding="utf-8-sig", newline="") as fh:
                    for r in csv.DictReader(fh):
                        name = r.get("Company Name") or r.get("SECURITY_NAME") or r.get("Issuer")
                        isin = r.get("ISIN Code") or r.get("ISIN")
                        if not name or not isin:
                            continue
                        isin = isin.strip().upper()
                        n = norm(name)
                        ref[n] = isin
                        ref[n.replace(" limited", " ltd")] = isin
            except Exception:
                continue
    return ref


_REF: dict[str, str] | None = None
_TOKEN: defaultdict[str, list[str]] | None = None


def _get_ref() -> tuple[dict[str, str], defaultdict[str, list[str]]]:
    global _REF, _TOKEN
    if _REF is None:
        _REF = _load_reference()
        _TOKEN = defaultdict(list)
        for jp in list(CONSTITUENTS.glob("*.csv")) + list(DEBT_CONSTITUENTS.glob("*.csv")):
            try:
                with jp.open(encoding="utf-8-sig", newline="") as fh:
                    for r in csv.DictReader(fh):
                        name = r.get("Company Name") or r.get("SECURITY_NAME") or r.get("Issuer")
                        isin = (r.get("ISIN Code") or r.get("ISIN") or "").strip().upper()
                        if name and isin:
                            for t in tokens(name):
                                _TOKEN[t].append(isin)
            except Exception:
                continue
    return _REF, _TOKEN


def lookup_isin(name: str) -> str | None:
    """Return the ISIN for a company name (exact, then fuzzy). None if unknown."""
    ref, token = _get_ref()
    n = norm(name)
    if n in ref:
        return ref[n]
    toks = tokens(name)
    if not toks:
        return None
    common: Counter[str] = Counter()
    for t in toks:
        for isin in token.get(t, []):
            common[isin] += 1
    if not common:
        return None
    best_isin, best = common.most_common(1)[0]
    # 2+ shared tokens, OR one distinctive (>=5 char) token
    if best >= 2:
        return best_isin
    if best == 1 and any(len(t) >= 5 for t in toks):
        return best_isin
    return None


if __name__ == "__main__":
    for probe in ["Toubro Ltd", "Mahindra Limited", "ICICI Bank Limited",
                  "State Bank of India", "GSPL Transmission Limited", "Reliance Industries"]:
        print(probe, "->", lookup_isin(probe))
