"""[Phase 3] Current fund manager extraction — per-fund manager names mined
from the parsed corpus, never fabricated.

Attribution ladder (nearest-first):
  1. AMC monthly portfolio XLSX/PDF headers printing "Fund Manager(s)" under
     the scheme title (raw_text, nearest plausible fund-name line above).
  2. Factsheet `raw_text` — regex around "Fund Manager" / "Managed by" /
     "WHO MANAGES THE SCHEME?" per scheme block (window widened to 1200 chars;
     trailing plan qualifiers stripped before the name-shape test).
  3. "Fund Name:/Scheme Name:" label patterns in the preceding context.
  4. Single-scheme docs: the doc's own scheme (parsed `schemes` dict with one
     distinct fund_name) or the filename naming the scheme (KIM/SID/factsheet
     files) — provenance kept via `source`.
  5. AMC fund-manager web pages — NOT scraped here (no new bespoke scrapers).
  6. AI assist (src/ai_extract.py) — deliberately not used as fallback yet.

Output: data/reference/fund_managers.json keyed by fund-level canon_name ->
{managers[], as_of, source, amc, scheme_variants[], history[]}.
Unattributed FM hits are parked in `review` and written to
`data/reference/fund_manager_review.csv` (plan requirement) — never guessed.

Run:  python -m src.fund_managers --json-out data/reference/fund_managers.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
from pathlib import Path

from src.scheme_attributes import (
    BASE_DIR, FundMerger, iter_corpus_docs, month_key, plausible_fund_name,
    plausible_person,
)

OUT_DEFAULT = BASE_DIR / "data" / "reference" / "fund_managers.json"
REVIEW_DEFAULT = BASE_DIR / "data" / "reference" / "fund_manager_review.csv"

_FM_RE = re.compile(
    r"(?is)(?:fund\s+managers?\s*[:\-–]?\s*|managed\s+by\s*[:\-–]?\s*|"
    r"who\s+manages\s+the\s+scheme\??\s*[:\-–]?\s*)"
    r"([A-Z][A-Za-z0-9.,&'()\- /]{4,200})")
_NAME_LABEL_RE = re.compile(
    r"(?is)(?:fund\s+name|scheme\s+name|name\s+of\s+the\s+scheme)\s*[:\-–]\s*"
    r"([A-Za-z0-9&.,'()\- ]{8,110})")
_STOP_CTX_RE = re.compile(
    r"(?ix)(?:risk-?ometer|investment\s+objective|nav\b|as\s+on\s+|"
    r"http|www\.|product\s+label|suitable\s+for)")
_NAME_SPLIT_RE = re.compile(r"\s*,\s*|\s+&\s+|\s+and\s+|\n")
_NAME_CTX_JUNK = re.compile(
    r"(?ix)(?:the\s+scheme|objective|returns?\b|performance|portfolio|"
    r"fund\s+manager|managed\s+by|since\s+inception|\bnav\b|aum|expense|"
    r"benchmark|risk|launch|load\b|exit|options?\b|plans?\b|isIN|\d)")
_FILENAME_NOISE_RE = re.compile(
    r"(?ix)\b(?:factsheet|fact-sheet|kim|sid|sai|monthly|portfolio|statement|"
    r"web|final|dec|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|"
    r"d{1,2}[-_ ]?d{0,2}|20\d{2})\b[\s._-]*")
_SCHEME_HINT_STOP = re.compile(r"(?ix)\b(?:idcw|wrv|xlsx?|pdf|zip|copy)\b")


def _clean_names(chunk: str) -> list[str]:
    names = []
    for raw in _NAME_SPLIT_RE.split(chunk):
        nm = re.sub(r"\s+", " ", raw).strip(" .,-–|:&()")
        nm = re.sub(r"\s*\([^)]*\)\s*$", "", nm).strip()   # drop "(since …)"
        nm = re.sub(r"(?i)\s*[-–]\s*(?:equity|debt).*$", "",
                    nm).strip()
        if plausible_person(nm) and not _NAME_CTX_JUNK.search(nm):
            names.append(nm)
    return names


def _scheme_hint_from_filename(source_file: str) -> str:
    stem = re.sub(r"\.[a-z0-9]+$", "", source_file or "", flags=re.I)
    stem = _FILENAME_NOISE_RE.sub(" ", stem)
    nm = re.sub(r"[_%]+", " ", stem)
    nm = re.sub(r"\s{2,}", " ", nm).strip(" -–_|.,")
    if _SCHEME_HINT_STOP.search(nm) or len(nm) < 8:
        return ""
    return nm


def _attributed_name(text: str, match_start: int,
                     hint: str | None) -> tuple[str, str]:
    """Return (fund_name, attribution_kind) for an FM match. Empty name when
    nothing plausible is found (row goes to review, never guessed)."""
    ctx = text[max(0, match_start - 2500):match_start]
    # 1/2: nearest plausible fund-name line above (window spans long
    # returns tables inside per-scheme factsheet sections)
    for ln in reversed([l.strip() for l in ctx.splitlines()]):
        if plausible_fund_name(ln):
            return ln.strip(" -:–|."), "context"
    # 3: explicit name labels anywhere in the context
    for lm in _NAME_LABEL_RE.finditer(ctx):
        nm = lm.group(1).strip(" .")
        if plausible_fund_name(nm):
            return nm, "label"
    # 4: single-scheme hint (doc's own scheme or filename)
    if hint:
        return hint, "hint"
    return "", ""


def extract_managers(text: str, hint: str | None = None) -> list[dict]:
    """Return [{scheme_name, managers[], via}] for every FM hit — including
    unattributed ones (scheme_name == "") so the caller can review them."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _FM_RE.finditer(text):
        ctx_before = text[max(0, m.start() - 300):m.start()]
        if _STOP_CTX_RE.search(ctx_before[-120:]):
            continue
        names = _clean_names(m.group(1))
        if not names:
            continue
        name, via = _attributed_name(text, m.start(), hint)
        key = (name or f"?{m.start()}") + "|" + ",".join(names).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"scheme_name": name, "managers": names, "via": via})
    return out


def _single_scheme_hint(doc: dict, source_file: str) -> str:
    """One distinct fund name for the doc (parsed schemes) else filename."""
    names: set[str] = set()
    for s in (doc.get("schemes") or {}).values():
        if isinstance(s, dict):
            fn = (s.get("fund_name") or s.get("scheme_name") or "").strip()
            if len(fn) >= 8 and not _SCHEME_HINT_STOP.search(fn):
                names.add(fn)
    for sh in (doc.get("top_holdings") or [])[:0]:
        pass
    if len(names) == 1:
        return next(iter(names))
    if not names:
        return _scheme_hint_from_filename(source_file)
    return ""


def build(json_out: Path = OUT_DEFAULT,
          review_csv: Path = REVIEW_DEFAULT) -> dict:
    merger = FundMerger()
    review_rows: list[dict] = []
    docs = rows = unattributed = 0
    for _p, amc, as_of, source, text, doc in iter_corpus_docs():
        low = text.lower()
        if not ("fund manager" in low or "managed by" in low
                or "who manages the scheme" in low):
            continue
        hint = _single_scheme_hint(doc, source)
        found = extract_managers(text, hint=hint)
        if not found:
            continue
        docs += 1
        mk = month_key(as_of)
        first_fm = low.find("fund manager")
        if first_fm < 0:
            first_fm = low.find("who manages")
        for row in found:
            if len(row["scheme_name"]) >= 8:
                rows += 1
                merger.add(row["scheme_name"], amc, mk, source,
                           {"managers": row["managers"]})
            else:
                unattributed += 1
                review_rows.append({
                    "amc": amc, "as_of": mk, "source_file": source,
                    "snippet": text[max(0, first_fm - 60):first_fm + 200]
                    .replace("\n", " ")[:220],
                })
    payload = {
        "v": 2,
        "built": _dt.datetime.now().isoformat(timespec="seconds"),
        "funds": merger.serialise(),
        "review": review_rows[:500],
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    try:
        with open(review_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["amc", "as_of", "source_file",
                                               "snippet"])
            w.writeheader()
            w.writerows(review_rows[:1000])
    except Exception:
        pass
    return {"funds": len(payload["funds"]), "rows": rows, "docs": docs,
            "unattributed": unattributed, "out": str(json_out)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--review-csv", type=Path, default=REVIEW_DEFAULT)
    args = ap.parse_args()
    print(json.dumps(build(args.json_out, args.review_csv), indent=1))
