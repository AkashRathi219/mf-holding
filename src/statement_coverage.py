"""Statement coverage matrix [stmt-cov-v1.0.0].

Which audited/unaudited quarterly and annual filings exist — and which were
already parsed into `data/stock_financials/` — for every tracked equity over
the last five fiscal years. Two inputs, both local:

- parsed docs   data/stock_financials/<ISIN>.json   (records carry fy, kind,
  quarter, audit — audit added by the pull_annual_results pipeline)
- filed sidecar data/raw/financial_results/_inventory/<SYMBOL>.json  (every
  result announcement considered during the pull, with audit + period hints)

Cells never mix the two: a filing can be filed-but-not-parsed (PDF downloaded
or merely seen in the announcement feed) or parsed. Nothing is fabricated for
gaps — absent cells simply mean no filing is known for that slot.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import statement_schema as ss
from .stock_identity import load_identity

BASE_DIR = Path(__file__).resolve().parents[1]
FINANCIALS_DIR = BASE_DIR / "data" / "stock_financials"
INVENTORY_DIR = BASE_DIR / "data" / "raw" / "financial_results" / "_inventory"

QUARTERS = ("Q1", "Q2", "Q3", "Q4")
PERIODS = ("Q1", "Q2", "Q3", "Q4", "FY")
AUDITS = ("Audited", "Unaudited", "unknown")

_cache: dict = {"key": None, "payload": None}


def window_fys(n: int = 5) -> list[str]:
    """Last n completed Indian fiscal years, oldest first. Aug-2026 -> FY22..FY26."""
    today = datetime.today()
    latest = today.year if today.month >= 4 else today.year - 1
    return [f"FY{str(latest - n + 1 + i)[-2:]}" for i in range(n)]


def _cell(fy: str, period: str, audit: str | None) -> str:
    return f"{fy}|{period}|{audit or 'unknown'}"


def _parsed_cells(doc: dict, fys: list[str]) -> dict[str, int]:
    block = doc.get("consolidated") or doc.get("standalone") or {}
    cells: dict[str, int] = {}
    for rec in (block.get("quarters") or []):
        if not isinstance(rec, dict) or rec.get("cumulative"):
            continue
        fy, q = rec.get("fy"), rec.get("quarter")
        if fy in fys and q in QUARTERS:
            cells[_cell(fy, q, rec.get("audit"))] = \
                cells.get(_cell(fy, q, rec.get("audit")), 0) + 1
    for rec in (block.get("annual") or []):
        if not isinstance(rec, dict):
            continue
        fy = rec.get("fy")
        if fy in fys and rec.get("kind") == "FY":
            cells[_cell(fy, "FY", rec.get("audit"))] = \
                cells.get(_cell(fy, "FY", rec.get("audit")), 0) + 1
    return cells


def _filed_cells(inv: dict, fys: list[str]) -> dict[str, int]:
    cells: dict[str, int] = {}
    for a in (inv.get("announcements") or []):
        per = ss.primary_period_from_headline(a.get("headline") or "")
        if not per:
            continue
        kind, d = per
        if kind == "Q":
            period = ss.quarter_of_month(d[1], "Q")
        elif kind == "FY":
            period = "FY"
        else:
            continue                      # H1/9M cumulative results
        fy = ss.fiscal_year(d, kind)
        if fy not in fys or not period:
            continue
        key = _cell(fy, period, a.get("audit"))
        cells[key] = cells.get(key, 0) + 1
    return cells


def build_coverage(max_stale_seconds: float = 0.0) -> dict:
    """Coverage payload over every tracked equity (identity map). Cached in
    the webapp process; scripts call it with the default no-cache."""
    import time as _time
    if max_stale_seconds and _cache["payload"] is not None \
            and _cache["key"] and \
            _time.time() - _cache["key"] < max_stale_seconds:
        return _cache["payload"]

    fys = window_fys(5)
    ident = load_identity()
    stocks: list[dict] = []
    summary_cells: dict[str, int] = {}
    with_doc = 0
    for isin, row in sorted(ident.items(),
                            key=lambda kv: (kv[1].get("symbol") or "",
                                            kv[0])):
        symbol = row.get("symbol") or ""
        parsed: dict[str, int] = {}
        filed: dict[str, int] = {}
        doc_available = False
        doc_path = FINANCIALS_DIR / f"{isin}.json"
        if doc_path.exists():
            try:
                doc = json.loads(doc_path.read_text(encoding="utf-8"))
            except Exception:
                doc = None
            if isinstance(doc, dict) and \
                    (doc.get("consolidated") or doc.get("standalone")):
                doc_available = True
                with_doc += 1
                parsed = _parsed_cells(doc, fys)
        if symbol:
            inv_path = INVENTORY_DIR / f"{symbol}.json"
            if inv_path.exists():
                try:
                    inv = json.loads(inv_path.read_text(encoding="utf-8"))
                except Exception:
                    inv = {}
                filed = _filed_cells(inv if isinstance(inv, dict) else {}, fys)
        for k, n in parsed.items():
            summary_cells[k] = summary_cells.get(k, 0) + n
        stocks.append({
            "isin": isin, "symbol": symbol, "name": row.get("name") or "",
            "doc_available": doc_available,
            "parsed": parsed, "filed": filed,
        })
    payload = {
        "version": "stmt-cov-v1.0.0",
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "window_fys": fys,
        "periods": PERIODS,
        "universe": len(ident),
        "with_doc": with_doc,
        "summary": summary_cells,
        "stocks": stocks,
    }
    _cache["key"] = _time.time()
    _cache["payload"] = payload
    return payload


def csv_rows(payload: dict) -> tuple[list[str], list[list]]:
    """Long-format rows: one per stock x FY x period x audit class."""
    header = ["symbol", "isin", "fy", "period", "audit",
              "parsed", "filed", "status"]
    rows: list[list] = []
    for s in payload["stocks"]:
        for fy in payload["window_fys"]:
            for period in payload["periods"]:
                for audit in AUDITS:
                    key = f"{fy}|{period}|{audit}"
                    p = s["parsed"].get(key, 0)
                    f = s["filed"].get(key, 0)
                    if not p and not f:
                        continue
                    rows.append([s["symbol"], s["isin"], fy, period, audit,
                                 p, f,
                                 "parsed" if p else "filed_not_parsed"])
    return header, rows
