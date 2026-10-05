"""Shared foundation for the scheme-attribute modules (riskometer,
descriptions, fund managers) — corpus walking, provenance, fund-level
canonical merging and SEBI risk-level normalisation.

Outputs are keyed by fund-level ``canon_name`` (plan-stripped, brand-
normalised) so Scheme Details can look attributes up regardless of the
plan/option variant the AMC printed.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PARSED_DIR = BASE_DIR / "data" / "parsed" / "amc_websites"

# SEBI's 6-point risk scale (CIR/IMD/DF/11/2010 + product-labelling updates).
SEBI_LEVELS = (
    "low",
    "low to moderate",
    "moderate",
    "moderately high",
    "high",
    "very high",
)

_RISK_JUNK_RE = re.compile(
    r"(?i)(?:an\s+)?open\s+ended|closed\s+ended|relatively\s+higher|"
    r"relatively\s+lower|risk[- ]?ometer|benchmark|investors?|"
    r"principal\s+|this\s+product|suitable"
)

# Section headers that terminate an "Investment objective" block.
_SECTION_BREAK_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"portfolio|asset\s+allocation|performance|returns|nav|net\s+asset\s+value|"
    r"fund\s+manager|fund\s+managers|managed\s+by|riskometer|risk\s+parameters|"
    r"product\s+label|suitable|benchmark|load|exit\s+load|entry\s+load|"
    r"minimum|plans?\s+available|options?\s+available|features|statistics|"
    r"launch|date\s+of\s+allotment|aum|expense|ter\b|portfolio\s+turnover|"
    r"fund\s+size|trustee|registrar|custodian|inception"
    r")"
)

# Text that disqualifies a captured name from being a plausible fund name.
_FUND_NAME_STOP_RE = re.compile(
    r"(?ix)(?:risk-?ometer|benchmark|investment\s+objective|nav\b|"
    r"as\s+on\s+|as\s+of\s+|http|www\.|\d{2}[-/]\d{2}[-/]\d{4})"
)

# Person-name validation (fund managers): 2-4 alphabetic tokens, each
# title-cased-ish, no keyword junk.
_MANAGER_JUNK_RE = re.compile(
    r"(?ix)(?:sip|swp|stp|nav|aum|ter|fof|etf|idcw|fund|scheme|portfolio|"
    r"managed|manager|equity|debt|arbitrage|growth|plan|direct|regular|"
    r"objective|return|benchmark|risk|since|inception|note|as\s+on|http)"
)

# Section-header words that disqualify a line from being a fund name.
_SECTION_WORD_RE = re.compile(
    r"(?ix)\b(?:features?|snapshot|overview|structure|analysis|statistics|"
    r"disclaimer|contents|annexure|portfolio\b(?!.*fund)|monthly|"
    r"product\s+labell?ing|performance|returns?)\b"
)


def plausible_fund_name(line: str) -> bool:
    """True when a raw_text line is a plausible scheme/fund name (used to
    attribute mined attributes to the right fund)."""
    if not line:
        return False
    s = line.strip()
    if not 8 <= len(s) <= 110 or _FUND_NAME_STOP_RE.search(s):
        return False
    if _SECTION_WORD_RE.search(s):
        return False
    # trailing qualifiers don't disqualify: "(Direct Plan)", "- Growth", "&"
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"(?i)\s*[-–—|]\s*(?:direct|regular)\s+plan.*$", "", s)
    s = re.sub(r"(?i)\s*[-–—|:]\s*(?:direct|regular|growth|idcw|dividend|"
               r"monthly|weekly|daily|payout|reinvestment|bonus|option)\b.*$",
               "", s)
    # must LOOK like a name: ends with a fund-ish noun (rejects objective
    # taglines like "An open-ended dynamic equity scheme investing acro…")
    if not re.search(
            r"(?i)\b(?:fund|scheme|plan|etf|fof|index|nifty(?:\s+\w+)?|"
            r"gilt|growth|dividend|option|idcw)\s*$", s):
        return False
    return True


def normalise_risk_label(label: str | None) -> str | None:
    """Normalise an AMC riskometer label to one of the 6 SEBI levels.

    Returns None for anything outside the scale — the caller must park it
    for review, never guess (plan rule: 0 fabricated levels).
    """
    if not label:
        return None
    s = _RISK_JUNK_RE.sub(" ", str(label))
    s = re.sub(r"[^A-Za-z ]+", " ", s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    # tolerant containment (e.g. "Moderately High (Relatively Higher)")
    for level in SEBI_LEVELS:
        if s == level or (level in s and len(s) <= len(level) + 12):
            return level
    if s in {"low-moderate", "low to moderat", "lowmoderate"}:
        return "low to moderate"
    return None


def plausible_person(name: str) -> bool:
    """True when `name` looks like a real person's name (not junk tokens)."""
    if not name or _MANAGER_JUNK_RE.search(name):
        return False
    toks = [t for t in re.split(r"[^A-Za-z.]+", name) if t]
    if not 2 <= len(toks) <= 4:
        return False
    for t in toks:
        if len(t) < 2 or not t[0].isalpha():
            return False
        if t.islower():
            return False
    return True


def doc_as_of(doc: dict) -> str:
    """Best-effort as-of (YYYY-MM or full date) from parse metadata."""
    meta = doc.get("metadata") or {}
    for k in ("as_of", "date", "portfolio_date"):
        if meta.get(k):
            return str(meta[k])
    y, m = doc.get("fetch_year"), doc.get("fetch_month")
    if y and m:
        return f"{int(y):04d}-{int(m):02d}"
    return ""


def doc_amc(doc: dict, path: Path) -> str:
    amc = (doc.get("amc_name") or "").replace("_", " ").strip()
    if amc:
        return amc
    try:
        return path.relative_to(PARSED_DIR).parts[0].replace("_", " ")
    except (ValueError, IndexError):
        return path.parent.name.replace("_", " ")


def iter_corpus():
    """Yield (path, amc, as_of, source_file, raw_text) for text-bearing parses."""
    for item in iter_corpus_docs():
        path, amc, as_of, source, text, _doc = item
        yield path, amc, as_of, source, text


def iter_corpus_docs():
    """Like iter_corpus but also yields the parsed doc dict (6-tuple)."""
    if not PARSED_DIR.is_dir():
        return
    for p in sorted(PARSED_DIR.rglob("*.json")):
        if p.name.startswith("report_"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        text = doc.get("raw_text") or ""
        if not text:
            continue
        yield p, doc_amc(doc, p), doc_as_of(doc), \
            (doc.get("source_file") or p.name), text, doc


class FundMerger:
    """Merge per-document attributes into fund-level records keyed by canon."""

    def __init__(self) -> None:
        from webapp.db import canon_name  # late import avoids cycles
        self._canon = canon_name
        self.funds: dict[str, dict] = {}
        self.review: list[dict] = []

    def _record(self, canon: str, name: str, amc: str) -> dict:
        rec = self.funds.get(canon)
        if rec is None:
            rec = self.funds[canon] = {
                "fund_name": name, "amc": amc, "scheme_variants": set(),
                "history": [],
            }
        rec["scheme_variants"].add(name)
        return rec

    def add(self, fund_name: str, amc: str, as_of: str, source: str,
            attrs: dict) -> None:
        canon = self._canon(fund_name)
        if not canon:
            return
        rec = self._record(canon, fund_name, amc)
        entry = {"as_of": as_of, "source": source, **attrs}
        rec["history"].append(entry)
        # latest-wins for the headline values (as_of order: full date > YYYY-MM > "")
        cur = rec.get("as_of") or ""
        if len(as_of) >= len(cur) and as_of >= cur:
            rec.update({k: v for k, v in attrs.items()})
            rec["as_of"] = as_of
            rec["source"] = source

    def review_row(self, **row) -> None:
        self.review.append(row)

    def serialise(self) -> dict:
        out = {}
        for canon, rec in self.funds.items():
            variants = sorted(rec.pop("scheme_variants"))
            hist = sorted(rec.get("history") or [],
                          key=lambda e: e.get("as_of") or "")
            rec.pop("history", None)
            rec["scheme_variants"] = variants
            if hist:
                rec["history"] = hist
            out[canon] = rec
        return out


def month_key(as_of: str) -> str:
    """Coarse staleness key: '2026-07-06' / '2026-07' / '' -> '2026-07'."""
    m = re.match(r"(\d{4})-(\d{2})", as_of or "")
    return f"{m.group(1)}-{m.group(2)}" if m else ""
