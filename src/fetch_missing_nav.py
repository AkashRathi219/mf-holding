"""Download NAV history for scheme codes that lack a ``data/nav_history/<code>.json``
file but are referenced by a scheme in the webapp database.

Why these are missing
---------------------
The AMFI ``nav_history`` backfill (``src/nav_history.py``) only targets the codes in
the curated universe CSV. Codes that come from the daily NAVAll snapshot (ETFs,
new funds, etc.) or from plan-level universe rows are never downloaded, so their
NAV chart shows "No NAV history available".

Source
------
``api.mfapi.in/mf/<code>`` — a free, open mirror of the official AMFI NAV history.
One request returns the full daily history for a single scheme code (much faster
than the 90-day-window AMFI backfill for a targeted set of codes).

Run::

    python -m src.fetch_missing_nav                 # download all missing codes
    python -m src.fetch_missing_nav --codes 149392  # download specific codes
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "webapp.db"
NAV_HISTORY_DIR = BASE_DIR / "data" / "nav_history"

API = "https://api.mfapi.in/mf/{code}"
UA = {"User-Agent": "Mozilla/5.0 (FactsheetEngineAI nav backfill)"}

_MONTHS = {f"{i:02d}": m for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
# [BUG-C3] inverse map for _date_key: input months are NAMES ("Aug"), not
# numbers — the old lookup passed them through and sort keys came out as
# '2026-Aug-18' (alphabetical month order, not chronological).
_MONTH_NUM = {v: k for k, v in _MONTHS.items()}


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


def _fetch(code: str) -> dict | None:
    """Fetch full history for one scheme code. Returns None on failure."""
    req = urllib.request.Request(API.format(code=code), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
            doc = json.load(resp)
    except Exception:
        return None
    if doc.get("status") != "SUCCESS":
        return None
    data = doc.get("data") or []
    if not data:
        return None
    return doc


def _convert_date(dd_mm_yyyy: str) -> str:
    """'18-08-2026' -> '18-Aug-2026' (matches existing nav_history format)."""
    try:
        day, mon, year = dd_mm_yyyy.split("-")
        return f"{int(day):02d}-{_MONTHS.get(mon, mon)}-{year}"
    except Exception:
        return dd_mm_yyyy


def _build_doc(code: str, resp: dict) -> dict:
    meta = resp.get("meta") or {}
    rows = resp.get("data") or []
    # mfapi returns newest-first; history files are stored oldest-first.
    history = sorted(
        ({"date": _convert_date(r.get("date", "")),
          "nav": float(r.get("nav", 0))} for r in rows
         if r.get("date") and r.get("nav") not in (None, "")),
        key=lambda h: _date_key(h["date"]))
    return {
        "scheme_code": code,
        "fund_name": meta.get("scheme_name") or "",
        "category": meta.get("scheme_category") or "",
        "plan": "",
        "option": "",
        "isin": meta.get("isin_growth") or "",
        "isin_reinvestment": meta.get("isin_div_reinvestment") or "",
        "currency": "INR",
        "source": "AMFI",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "history": history,
    }


def _date_key(datestr: str) -> str:
    """'18-Aug-2026' -> '2026-08-18' for chronological sorting."""
    try:
        day, mon, year = datestr.split("-")
        return f"{year}-{_MONTH_NUM.get(mon.title(), _MONTH_NUM.get(mon, mon))}-{int(day):02d}"
    except Exception:
        return datestr


def missing_codes() -> list[str]:
    """All scheme codes referenced by the webapp DB that lack a history file."""
    import sqlite3
    codes: set[str] = set()
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        try:
            for reg, dirc in con.execute(
                    "SELECT amfi_regular, amfi_direct FROM schemes"):
                for c in (reg, dirc):
                    if c and not (NAV_HISTORY_DIR / f"{c}.json").exists():
                        codes.add(c)
        finally:
            con.close()
    return sorted(codes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="*", default=None,
                        help="specific codes to fetch (default: all missing)")
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    NAV_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    codes = args.codes or missing_codes()
    print(f"Fetching NAV history for {len(codes)} codes...", flush=True)

    ok = failed = skipped = 0
    for i, code in enumerate(codes, 1):
        out = NAV_HISTORY_DIR / f"{code}.json"
        if out.exists():
            skipped += 1
            continue
        resp = _fetch(code)
        if resp is None:
            failed += 1
            print(f"[{i}/{len(codes)}] {code}: NO DATA", flush=True)
            time.sleep(args.delay)
            continue
        doc = _build_doc(code, resp)
        if not doc["history"]:
            failed += 1
            print(f"[{i}/{len(codes)}] {code}: empty history", flush=True)
            time.sleep(args.delay)
            continue
        out.write_text(json.dumps(doc), encoding="utf-8")
        ok += 1
        if i % 25 == 0 or i == len(codes):
            print(f"[{i}/{len(codes)}] ok={ok} failed={failed} skipped={skipped}",
                  flush=True)
        time.sleep(args.delay)

    print(f"\nDone: ok={ok} failed={failed} skipped={skipped}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
