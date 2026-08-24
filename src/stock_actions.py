"""Corporate-actions agent (dividends + splits) per stock.

Produces ``data/stock_actions/<ISIN>.json`` = ``{isin, symbol, name, fetched_at,
dividends: [{date, amount}], splits: [{date, ratio}]}``.

Sources
-------
1. **Yahoo Finance events** — ``v8/finance/chart/<SYMBOL>.NS?events=div,split``
   returns dated dividend amounts and split ratios (reliable, no auth).
2. **NSE corporate announcements** — filtered to dividend/bonus/split keywords as
   a supplementary view (``nseindia.com/api/corporate-announcements``).

Run::

    python -m src.stock_actions                 # all resolved stocks
    python -m src.stock_actions --symbols ADANIENT
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from .stock_common import (ACTIONS_DIR, date_key, http_get, load_json, nse_session,
                           now_iso, save_json)
from .stock_identity import load_identity

YAHOO_EVENTS_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}?"
                    "range=max&interval=1d&events=div%2Csplit")
NSE_ANNOUNCE_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}")

_DIV_KEYWORDS = re.compile(r"dividend|interim dividend|final dividend|bonus|split|rights", re.I)


def _fetch_yahoo_events(symbol: str) -> tuple[list[dict], list[dict]]:
    """Returns (dividends, splits) from Yahoo events."""
    try:
        raw = http_get(YAHOO_EVENTS_URL.format(sym=f"{symbol}.NS"), timeout=30)
        result = json.loads(raw.decode("utf-8", "replace"))["chart"]["result"][0]
        events = result.get("events") or {}
        dividends = []
        for ts, e in (events.get("dividends") or {}).items():
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            dividends.append({"date": d.strftime("%d-%b-%Y"),
                              "amount": round(float(e.get("amount") or 0), 4)})
        splits = []
        for ts, e in (events.get("splits") or {}).items():
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            splits.append({"date": d.strftime("%d-%b-%Y"),
                           "ratio": f"{e.get('numerator')}:{e.get('denominator')}"})
        dividends.sort(key=lambda x: date_key(x["date"]))
        splits.sort(key=lambda x: date_key(x["date"]))
        return dividends, splits
    except Exception:
        return [], []


def _fetch_nse_announcements(symbol: str) -> list[dict]:
    """Best-effort NSE corporate announcements mentioning dividends/splits.

    Uses the cookie-warmed NSE session and a single fast retry — when Akamai
    blocks the call we skip the symbol instead of burning ~14s of backoff
    (868 symbols x retries was hanging the refresh for hours)."""
    out = []
    try:
        raw = http_get(NSE_ANNOUNCE_URL.format(sym=symbol),
                       headers={"Referer": "https://www.nseindia.com/"},
                       timeout=20, opener=nse_session(), retries=1)
        data = json.loads(raw.decode("utf-8", "replace"))
        for a in data or []:
            text = f"{a.get('attchmntText') or ''} {a.get('an_subject') or ''}"
            if _DIV_KEYWORDS.search(text):
                out.append({"date": (a.get("an_dt") or "")[:11],
                            "headline": text.strip()[:180],
                            "url": a.get("attchmntFile") or "",
                            "size": a.get("attFileSize") or ""})
    except Exception:
        pass
    return out[:20]


def refresh_actions(isin: str, ident: dict) -> dict:
    symbol = ident.get("symbol") or ""
    name = ident.get("name") or ""
    if not symbol:
        return {"isin": isin, "status": "no_symbol"}
    path = ACTIONS_DIR / f"{isin}.json"
    prev = load_json(path)
    dividends, splits = _fetch_yahoo_events(symbol)
    yahoo_ok = bool(dividends or splits)
    if yahoo_ok:
        announcements = _fetch_nse_announcements(symbol)
    else:
        # [BUG-H5] an empty Yahoo response is indistinguishable from a source
        # failure; overwriting would silently destroy stored dividend/split
        # history (and split-correction for future price re-backfills). Keep
        # the previously curated data instead of shrinking it.
        dividends = prev.get("dividends") or []
        splits = prev.get("splits") or []
        announcements = prev.get("announcements") or []
    doc = {"isin": isin, "symbol": symbol, "name": name,
           "fetched_at": now_iso(),
           "dividends": dividends, "splits": splits,
           "announcements": announcements}
    save_json(path, doc)
    return {"isin": isin, "symbol": symbol,
            "status": "ok" if yahoo_ok else "kept_previous",
            "dividends": len(dividends), "splits": len(splits)}


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
        out.append(refresh_actions(isin, ident_row))
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