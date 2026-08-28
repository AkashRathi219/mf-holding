"""Ingest the worker-downloaded NSE `output/<SYMBOL>/` CSVs into the
normalised statement documents the webapp renders
(`data/stock_financials/<ISIN>.json`).

Source of record here is the NSE results-comparision API dump
(`nse_financials_latest5q.csv`: last 5 filing periods of income-statement
line items). NSE reports amounts in Rs LAKH; the house statement schema is
Rs CRORE, so every flow value is divided by 100. Per-share items (EPS,
face value) stay in rupees.

Docs are merged, never clobbered: PDF-parsed records already present in a
document win over feed rows for the same period; only missing periods are
appended. TTM is recomputed over the merged quarter set.

Usage:
    python -m src.ingest_output_financials            # all output/ symbols
    python -m src.ingest_output_financials --symbol TCS,SBIN
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import statement_schema as ss  # noqa: E402
from src.financial_statements import (FINANCIALS_DIR,  # noqa: E402
                                      compute_ttm)
from src.stock_common import now_iso  # noqa: E402
from src.stock_identity import load_identity  # noqa: E402

OUT_DIR = os.path.join(os.getcwd(), "output")

# NSE results-comparision column -> canonical statement key. Flow items are
# Rs lakh on the feed and converted to crore; per-share keys are NOT scaled.
_FIELD_MAP = (
    ("revenue_from_operations", "revenue_from_operations"),
    ("total_income", "total_income"),
    ("total_expenses_excl_provisions", "total_expenses"),
    ("pbt", "pbt"),
    ("tax", "tax_expense"),
    ("net_profit", "pat"),
    ("basic_eps", "eps_basic"),
    ("diluted_eps", "eps_diluted"),
    ("face_value", "_face_value"),
)
_PER_SHARE = ss.PER_SHARE_KEYS

_SOURCE = "nse_results_comparision"


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _parse_dmy(s: str):
    try:
        return datetime.strptime((s or "").strip().upper(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _period_meta(period_end, period_start):
    """Indian fiscal-year label + quarter tag for an Apr-Mar year."""
    y, m = period_end.year, period_end.month
    fystart = y - 1 if m <= 3 else y
    fy = f"FY{str(fystart + 1)[-2:]}"
    q = {6: "Q1", 9: "Q2", 12: "Q3", 3: "Q4"}.get(m)
    span_days = (period_end - period_start).days if period_start else None
    return fy, q, span_days


def parse_latest5q_csv(path: str) -> tuple[list[dict], list[dict]]:
    """Parse the NSE feed CSV into (quarters, annuals) canonical records.

    Period classification by from/to span: ~3 months -> discrete quarter,
    ~12 months -> annual; anything in between is a cumulative YTD column
    (kept, but flagged so TTM and quarter series skip it). Rows without a
    single substantive value are dropped — never write empty shells.
    """
    quarters: list[dict] = []
    annuals: list[dict] = []
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            pe = _parse_dmy(row.get("to_date"))
            if pe is None:
                continue
            ps = _parse_dmy(row.get("from_date"))
            rec: dict = {"period_end": pe.isoformat(), "_source": _SOURCE}
            vals = {}
            for src, dst in _FIELD_MAP:
                v = _num(row.get(src))
                if v is None:
                    continue
                per_share = dst.lstrip("_") in _PER_SHARE
                vals[dst] = round(v / 100.0, 2) if not per_share \
                    else round(v, 2)
            if not vals:
                continue
            rec.update(vals)
            fy, q, span = _period_meta(pe, ps)
            rec["fy"] = fy
            if span is not None and span >= 330:
                rec["kind"] = "FY"
                annuals.append(rec)
            else:
                rec["kind"] = "Q"
                rec["quarter"] = q or ""
                if span is not None and span > 110:
                    rec["cumulative"] = True
                quarters.append(rec)
    quarters.sort(key=lambda r: r["period_end"])
    annuals.sort(key=lambda r: r["period_end"])
    return quarters, annuals


def _record_key(rec: dict):
    return (rec.get("kind"), rec.get("period_end"))


def merge_doc(existing: dict | None, symbol: str, isin: str,
              quarters: list[dict], annuals: list[dict]) -> tuple[dict, str]:
    """Merge feed records into an existing document (parsed records win) or
    build a fresh one. Returns (doc, action) where action is one of
    created / merged / unchanged."""
    doc: dict = json.loads(json.dumps(existing)) if existing else {
        "isin": isin, "symbol": symbol, "fetched_at": now_iso(),
        "source": _SOURCE, "units": "crore",
        "consolidated": {"quarters": [], "annual": [],
                         "basis_note": "NSE results-comparision feed "
                                       "(basis as last filed)"},
    }
    block = doc.get("consolidated") or doc.get("standalone")
    if block is None:                      # existing doc without either block
        block = {"quarters": [], "annual": []}
        doc["consolidated"] = block
    block["quarters"] = list(block.get("quarters") or [])
    block["annual"] = list(block.get("annual") or [])
    have = {_record_key(r) for r in block["quarters"] + block["annual"]
            if isinstance(r, dict)}
    added = 0
    for rec in quarters + annuals:
        if _record_key(rec) in have:
            continue
        block["quarters" if rec["kind"] == "Q" else "annual"].append(rec)
        have.add(_record_key(rec))
        added += 1
    block["quarters"].sort(key=lambda r: r.get("period_end") or "")
    block["annual"].sort(key=lambda r: r.get("period_end") or "")
    block["ttm"] = compute_ttm(block["quarters"])
    if existing is None:
        return doc, "created"
    return doc, ("merged" if added else "unchanged")


def build_symbol(symbol: str, isin: str) -> tuple[dict, str] | None:
    sdir = os.path.join(OUT_DIR, symbol)
    csv_path = os.path.join(sdir, "nse_financials_latest5q.csv")
    if not os.path.exists(csv_path):
        return None
    quarters, annuals = parse_latest5q_csv(csv_path)
    if not quarters and not annuals:
        return None
    existing = None
    doc_path = FINANCIALS_DIR / f"{isin}.json"
    if doc_path.exists():
        try:
            with open(doc_path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except Exception:
            existing = None
    return merge_doc(existing, symbol, isin, quarters, annuals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="comma-separated subset of symbols")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    want = {s.strip().upper() for s in (args.symbol or "").split(",")
            if s.strip()}

    ident = load_identity()
    sym_to_isin: dict[str, str] = {}
    for isin, row in ident.items():
        sym = (row.get("symbol") or "").upper()
        if sym and sym not in sym_to_isin:
            sym_to_isin[sym] = isin

    symbols = sorted({d for d in os.listdir(OUT_DIR)
                      if os.path.isdir(os.path.join(OUT_DIR, d))})
    if want:
        symbols = [s for s in symbols if s in want]

    FINANCIALS_DIR.mkdir(parents=True, exist_ok=True)
    tally: dict[str, int] = {}
    skipped: list[str] = []
    for sym in symbols:
        isin = sym_to_isin.get(sym)
        if not isin:
            skipped.append(sym)
            continue
        out = build_symbol(sym, isin)
        if out is None:
            tally["no_data"] = tally.get("no_data", 0) + 1
            continue
        doc, action = out
        tally[action] = tally.get(action, 0) + 1
        if action != "unchanged" and not args.dry_run:
            doc["fetched_at"] = doc.get("fetched_at") or now_iso()
            with open(FINANCIALS_DIR / f"{isin}.json", "w",
                      encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1, ensure_ascii=False)
    print(json.dumps(tally))
    if skipped:
        print("no ISIN mapping for:", ", ".join(skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
