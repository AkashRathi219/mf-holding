"""[Phase 5] Scheme Riskometer extraction — the AMC-assigned 6-level scheme
and benchmark risk, normalised against SEBI's scale (never guessed).

Sources, in precedence order:
  1. Dedicated monthly riskometer disclosures (routed here by main.py instead
     of the holdings parser — ``parse_document``).
  2. Factsheet / portfolio raw_text rows in the parsed corpus (``build``).

Output: data/reference/scheme_riskometer.json keyed by fund-level canon_name
-> {scheme_risk, benchmark_risk, as_of, source, amc, scheme_variants[],
history[]}. Non-normalisable labels are parked in `review`, never guessed.

Run:  python -m src.scheme_riskometer --json-out data/reference/scheme_riskometer.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path

from src.scheme_attributes import (
    BASE_DIR, FundMerger, iter_corpus, normalise_risk_label, month_key,
    plausible_fund_name, plausible_person,  # noqa: F401 (re-exported)
)

OUT_DEFAULT = BASE_DIR / "data" / "reference" / "scheme_riskometer.json"

_LABEL_RE = re.compile(
    r"(?i)(very\s+high|moderately\s+high|low\s+to\s+moderate|moderate|"
    r"low(?:\s+moderate)?|high)")
_SCHEME_RISK_RE = re.compile(
    r"(?is)scheme\s+risk-?ometer\s*[:\-–]?\s*([A-Za-z ()]{3,40}?)"
    r"(?:\s*(?:benchmark|risk-?ometer|$))")
_BENCH_RISK_RE = re.compile(
    r"(?is)benchmark\s+risk-?ometer\s*[:\-–]?\s*([A-Za-z ()]{3,40})")
_JUNK_SCHEME_RE = re.compile(
    r"(?ix)(?:^\s*(?:the\s+)?(?:scheme|fund)\s*$|risk-?ometer|benchmark|"
    r"investment\s+objective|as\s+on|\d{2}[-/]\d{2}|\bnav\b|http)")
# plausible fund-name tail before a risk label inside a table row
_ROW_SCHEME_RE = re.compile(
    r"(?is)([A-Za-z0-9&.,'()\[\] -]{8,90}?)\s*[-–:|\u2013]?\s+"
    r"(?:very\s+high|moderately\s+high|low\s+to\s+moderate|moderate|"
    r"low|high)\b")


_ROW_JUNK_RE = re.compile(
    r"(?ix)^(?:the\s+)?(?:credit\s+)?risk\b|risk\s+of\s+the\b|scheme\s+is\b|"
    r"relatively|colour|color|of\s+the\s+scheme$")


def _clean_scheme(name: str) -> str:
    s = re.sub(r"\s+", " ", name or "").strip(" -:–|.,")
    # strip mining artifacts: "Credit Risk of the <Fund> Relatively"
    s = re.sub(r"(?i)^.*?\brisk\s+of\s+the\s+", "", s)
    s = re.sub(r"(?i)\s+(?:relatively|higher|lower|moderate|high|low|"
               r"very\s+high)$", "", s).strip(" -:–|.,")
    s = re.sub(r"(?i)\b(plan|option|direct|regular|growth|idcw)\b\s*$", "",
               s).strip(" -:–|.,")
    return s


def _pair_from_labels(labels: list[tuple[int, str]], ctx: str) -> dict | None:
    """Two adjacent labels in a row -> (scheme_risk, benchmark_risk)."""
    if len(labels) < 2:
        return None
    # the row's scheme name is the text before the first label
    m = _ROW_SCHEME_RE.search(ctx)
    scheme = _clean_scheme(m.group(1)) if m else ""
    if not scheme or len(scheme) < 6:
        return None
    if _ROW_JUNK_RE.search(scheme) or _JUNK_SCHEME_RE.search(scheme):
        return None
    if not (plausible_fund_name(scheme)
            or plausible_fund_name(scheme.rstrip("() "))):
        return None
    return {
        "scheme_name": scheme,
        "scheme_risk": labels[0][1],
        "benchmark_risk": labels[1][1],
    }


def extract_levels(text: str) -> list[dict]:
    """Extract per-scheme (scheme_risk, benchmark_risk) rows from raw text.

    Only clean, normalisable rows survive; pictorial-dial garbage yields
    nothing (honest gap — levels are never inferred from the category).
    """
    out: list[dict] = []
    seen: set[str] = set()

    # Pattern 1: explicit "Scheme Riskometer: X / Benchmark Riskometer: Y"
    for sm, bm in zip(_SCHEME_RISK_RE.finditer(text),
                      _BENCH_RISK_RE.finditer(text)):
        lvl_s = normalise_risk_label(_LABEL_RE.match(sm.group(1).strip())
                                     .group(0) if _LABEL_RE.match(
                                         sm.group(1).strip()) else None)
        btxt = bm.group(1).strip()
        lvl_b = normalise_risk_label(
            _LABEL_RE.match(btxt).group(0) if _LABEL_RE.match(btxt) else None)
        if lvl_s and lvl_b:
            key = (sm.group(0)[:60], lvl_s, lvl_b)
            if key not in seen:
                seen.add(key)
                out.append({"scheme_name": "", "scheme_risk": lvl_s,
                            "benchmark_risk": lvl_b})

    # Pattern 2: table rows with two adjacent labels sharing a scheme name
    for line in re.split(r"(?:\n|(?<=\))\s{2,})", text):
        if "risk" not in line.lower() and not _LABEL_RE.search(line):
            continue
        labels = [(m.start(), m.group(0).lower()) for m in
                  _LABEL_RE.finditer(line)]
        if not 2 <= len(labels) <= 4:
            continue
        # collapse duplicates adjacent in position (e.g. "High High")
        compact: list[tuple[int, str]] = []
        for pos, lbl in labels:
            if compact and lbl == compact[-1][1] and pos - compact[-1][0] < 12:
                continue
            compact.append((pos, lbl))
        if len(compact) < 2:
            continue
        row = _pair_from_labels(compact, line)
        if not row:
            continue
        lv_s = normalise_risk_label(row["scheme_risk"])
        lv_b = normalise_risk_label(row["benchmark_risk"])
        if not lv_s or not lv_b:
            continue
        key = (row["scheme_name"].lower(), lv_s, lv_b)
        if key not in seen:
            seen.add(key)
            out.append({"scheme_name": row["scheme_name"],
                        "scheme_risk": lv_s, "benchmark_risk": lv_b})
    return out


def parse_document(path: str | Path) -> dict:
    """Parse a dedicated riskometer disclosure (PDF/text) — used by main.py
    instead of the holdings parser. Returns a payload with no `schemes`
    mapping, so the db loaders skip it."""
    path = Path(path)
    text = ""
    if path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = "\n".join((page.extract_text() or "")
                                 for page in pdf.pages)
        except Exception:
            text = ""
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = ""
    rows = extract_levels(text)
    return {
        "doc_type": "riskometer",
        "source_file": path.name,
        "riskometers": rows,
        "metadata": {},
    }


def build(json_out: Path = OUT_DEFAULT) -> dict:
    merger = FundMerger()
    docs = rows = 0
    for _p, amc, as_of, source, text in iter_corpus():
        if "riskometer" not in text.lower():
            continue
        found = extract_levels(text)
        if not found:
            continue
        docs += 1
        mk = month_key(as_of)
        for row in found:
            name = row["scheme_name"]
            if not name or len(name) < 6:
                continue
            rows += 1
            merger.add(name, amc, mk, source, {
                "scheme_risk": row["scheme_risk"],
                "benchmark_risk": row["benchmark_risk"],
            })
    payload = {
        "v": 1,
        "built": _dt.datetime.now().isoformat(timespec="seconds"),
        "funds": merger.serialise(),
        "review": merger.review[:500],
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    return {"funds": len(payload["funds"]), "rows": rows, "docs": docs,
            "out": str(json_out)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()
    print(json.dumps(build(args.json_out), indent=1))
