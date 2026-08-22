"""Recent financial reports / corporate announcements agent (NSE).

Produces ``data/stock_reports/<ISIN>.json`` = ``{isin, symbol, name, fetched_at,
announcements: [{date, headline, category, url, size}]}`` (latest ~20).

Source: NSE ``corporate-announcements`` API (``index=equities&symbol=<SYM>``),
filtered to the financial-results category. PDF attachments are linked by URL.

Run::

    python -m src.stock_reports                 # all resolved stocks
    python -m src.stock_reports --symbols ADANIENT
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .stock_common import REPORTS_DIR, http_get, load_json, now_iso, save_json
from .stock_identity import load_identity

NSE_ANNOUNCE_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}")

# Categories that count as "financial reports"
_FIN_CATS = ("financial results", "audited financial results", "unaudited financial",
             "quarterly results", "annual", "results", "profit")


def _is_financial(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _FIN_CATS)


def _fetch_nse(symbol: str) -> list[dict]:
    try:
        raw = http_get(NSE_ANNOUNCE_URL.format(sym=symbol),
                       headers={"Referer": "https://www.nseindia.com/"}, timeout=30)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return []
    out = []
    for a in data or []:
        text = f"{a.get('attchmntText') or ''} {a.get('an_subject') or ''}"
        if not _is_financial(text):
            continue
        out.append({
            "date": (a.get("an_dt") or "")[:11],
            "headline": text.strip()[:220],
            "category": "financial_results",
            "url": a.get("attchmntFile") or "",
            "size": a.get("attFileSize") or "",
        })
    return out[:20]


def refresh_reports(isin: str, ident: dict) -> dict:
    symbol = ident.get("symbol") or ""
    name = ident.get("name") or ""
    if not symbol:
        return {"isin": isin, "status": "no_symbol"}
    announcements = _fetch_nse(symbol)
    doc = {"isin": isin, "symbol": symbol, "name": name,
           "fetched_at": now_iso(), "announcements": announcements}
    save_json(REPORTS_DIR / f"{isin}.json", doc)
    return {"isin": isin, "symbol": symbol, "status": "ok", "reports": len(announcements)}


def run(ident: dict | None = None, symbols: list[str] | None = None,
        limit: int | None = None) -> list[dict]:
    ident = ident or load_identity()
    if symbols:
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in
                  {s.upper() for s in symbols}}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    if limit:
        target = dict(list(target.items())[:limit])
    out = []
    for i, (isin, ident_row) in enumerate(target.items(), 1):
        out.append(refresh_reports(isin, ident_row))
        if i % 25 == 0:
            print(f"  [{i}/{len(target)}]", flush=True)
        time.sleep(0.25)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    ident = load_identity()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    results = run(ident=ident, symbols=symbols, limit=args.limit)
    print(json.dumps(results[:10], indent=2))
    print(json.dumps({"total": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())