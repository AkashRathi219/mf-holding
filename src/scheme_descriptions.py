"""[Phase 5] Scheme short description extraction — the verbatim
"Investment Objective / scheme description" block per scheme, from factsheet
and portfolio raw_text in the parsed corpus.

Factual text only — the verbatim block is kept plus a trimmed display form
(first ~2 sentences); never paraphrased into marketing copy. Every record
carries provenance {description, display, source, as_of, amc}.

Output: data/reference/scheme_descriptions.json keyed by fund-level canon_name.

Run:  python -m src.scheme_descriptions --json-out data/reference/scheme_descriptions.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
from pathlib import Path

from src.scheme_attributes import (
    BASE_DIR, FundMerger, _SECTION_BREAK_RE, _FUND_NAME_STOP_RE, iter_corpus,
    month_key, plausible_fund_name,
)

OUT_DEFAULT = BASE_DIR / "data" / "reference" / "scheme_descriptions.json"

_OBJ_RE = re.compile(r"(?is)investment\s+objectives?\s*(?:of\s+the\s+"
                     r"scheme)?\s*[:\-–]?\n?")
# A candidate fund-name line: title-ish words incl. Fund/Scheme/Nifty etc.
_FUNDISH_RE = re.compile(
    r"(?i)(?:fund|scheme|nifty|sensex|etf|index|fof| gilt|bond|"
    r"equity|debt|hybrid|arbitrage|savings|cap\b)")
_MAX_BLOCK = 1600


def _trim_display(text: str, sentences: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(parts[:sentences]).strip()


def extract_objectives(text: str) -> list[dict]:
    """Return [{scheme_name, description}] blocks found in raw_text."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _OBJ_RE.finditer(text):
        start = m.end()
        rest = text[start:start + _MAX_BLOCK]
        # block ends at the next section header line
        stop = _SECTION_BREAK_RE.search(rest)
        block = (rest[:stop.start()] if stop else rest[:600]).strip()
        block = re.sub(r"\s*\n\s*", " ", block)
        block = re.sub(r"\s{2,}", " ", block).strip(" -–|.:")
        if len(block) < 60:          # too short to be a real objective
            continue
        if re.match(r"(?i)^(the\s+)?sincerely|signature|note\b", block):
            continue
        # nearest plausible fund name above the block
        head = text[max(0, m.start() - 400):m.start()]
        cand = ""
        for ln in reversed([l.strip() for l in head.splitlines()]):
            if plausible_fund_name(ln):
                cand = ln.strip(" -:–|.")
                break
        key = (cand or "").lower() + "|" + block[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"scheme_name": cand, "description": block})
    return out


def build(json_out: Path = OUT_DEFAULT) -> dict:
    merger = FundMerger()
    docs = rows = 0
    for _p, amc, as_of, source, text in iter_corpus():
        if "investment objective" not in text.lower():
            continue
        found = extract_objectives(text)
        if not found:
            continue
        docs += 1
        mk = month_key(as_of)
        for row in found:
            name = row["scheme_name"]
            if not name or len(name) < 8:
                continue
            rows += 1
            merger.add(name, amc, mk, source, {
                "description": row["description"],
                "display": _trim_display(row["description"]),
            })
    payload = {
        "v": 1,
        "built": _dt.datetime.now().isoformat(timespec="seconds"),
        "funds": merger.serialise(),
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
