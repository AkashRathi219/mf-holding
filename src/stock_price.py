"""Stock daily closing-price history agent (NSE bhavcopy primary).

Produces ``data/stock_history/<ISIN>.json`` = ``{isin, symbol, name, currency,
source, fetched_at, history: [{date, close, open, high, low, volume}]}``.

Fetch chain per stock (first success wins, later sources fill gaps):
1. **Manual CSV** - ``data/raw/stock_manual/<ISIN or SYMBOL>.csv``
   (``date,close`` or ``date,open,high,low,close,volume``). Authoritative when present.
2. **NSE bhavcopy archives (primary)** - one daily file covers every NSE-listed
   symbol (OHLC). Files are downloaded by a parallel pool of workers (one agent
   per daily file) and cached under ``data/stock_bhavcopy/``. Coverage starts
   ~2020 (the ``sec_bhavdata_full`` era); older dates are filled from the
   existing Yahoo history.
3. **Yahoo Finance** - fills pre-2020 history and any symbol the bhavcopy misses
   (delisted/odd series), full daily close for ``<SYMBOL>.NS``.
4. **Google Finance** - latest close only (no public history API).

For a backfill, the daily bhavcopy files are downloaded ONCE and reused for all
stocks (``run()`` builds an in-memory ``{date: {symbol: row}}`` index), so 868
stocks cost ~1,700 file downloads in total rather than 868 per-symbol calls.

Run::

    python -m src.stock_price                # backfill (bhavcopy primary)
    python -m src.stock_price --daily        # incremental append (daily)
    python -m src.stock_price --symbols ADANIENT,RELIANCE
    python -m src.stock_price --workers 12   # parallel bhavcopy downloaders
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .stock_common import (ACTIONS_DIR, HISTORY_DIR, MANUAL_DIR, date_key, http_get,
                           load_json, make_opener, norm_date, now_iso, save_json)
from .stock_identity import load_identity

BHAVCOPY_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "stock_bhavcopy"
BHAVCOPY_ARCHIVE_URL = ("https://archives.nseindia.com/products/content/"
                        "sec_bhavdata_full_{ddmmyyyy}.csv")
# Earliest reliably available "sec_bhavdata_full" file.
BHAVCOPY_START = date(2020, 1, 1)

YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}"
                   "&period2={p2}&interval=1d&crumb={crumb}")
GOOGLE_QUOTE_URL = "https://www.google.com/finance/quote/{sym}:NSE"


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _dedupe_sort(points: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for p in points:
        seen.setdefault(p["date"], p)
    return [seen[k] for k in sorted(seen, key=date_key)]


def _load_manual_csv(isin: str, symbol: str) -> list[dict] | None:
    candidates = [MANUAL_DIR / f"{isin}.csv", MANUAL_DIR / f"{isin}.CSV"]
    if symbol:
        candidates += [MANUAL_DIR / f"{symbol}.csv", MANUAL_DIR / f"{symbol}.CSV"]
    for path in candidates:
        if not path.exists():
            continue
        points = []
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    d = norm_date(r.get("date") or r.get("Date") or "")
                    if not d:
                        continue
                    close = _num(r.get("close") or r.get("Close"))
                    if close is None:
                        continue
                    points.append({
                        "date": d, "close": close,
                        "open": _num(r.get("open")),
                        "high": _num(r.get("high")),
                        "low": _num(r.get("low")),
                        "volume": _num(r.get("volume")),
                    })
        except Exception:
            continue
        if points:
            return _dedupe_sort(points)
    return None


# --------------------------------------------------------------------------
# NSE bhavcopy archives (primary) - one daily file covers all symbols
# --------------------------------------------------------------------------
def trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _bhav_path(d: date) -> Path:
    return BHAVCOPY_CACHE_DIR / (d.strftime("%Y%m%d") + ".csv")


def _download_bhavcopy_day(d: date) -> date | None:
    """Fetch + cache one daily bhavcopy file. Returns the date on success."""
    path = _bhav_path(d)
    if path.exists():
        # Reject cached HTML/PDF responses.
        try:
            raw = path.read_bytes()
            if raw[:6].startswith(b"SYMBOL"):
                return d
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
    url = BHAVCOPY_ARCHIVE_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
    try:
        raw = http_get(url, timeout=30, retries=2)
        if not raw[:6].startswith(b"SYMBOL"):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return d
    except Exception:
        return None


def download_bhavcopy_range(start: date, end: date, workers: int = 10) -> list[date]:
    """Download every trading-day bhavcopy in [start, end] using a pool of
    parallel workers (each worker is an independent download agent)."""
    days = trading_days(start, end)
    if not days:
        return []
    done: list[date] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_download_bhavcopy_day, d): d for d in days}
        for fut in as_completed(futures):
            if fut.result():
                done.append(futures[fut])
    return sorted(done)


def _parse_bhavcopy_day(d: date) -> dict[str, dict]:
    """Parse a cached daily file -> {symbol: {open, high, low, close, volume}}."""
    path = _bhav_path(d)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="latin-1", newline="") as fh:
            reader = csv.DictReader(fh)
            # NSE bhavcopy headers carry a leading space (' CLOSE_PRICE').
            reader.fieldnames = [c.strip() for c in (reader.fieldnames or [])]
            for r in reader:
                sym = (r.get("SYMBOL") or "").strip()
                if not sym:
                    continue
                close = _num(r.get("CLOSE_PRICE"))
                # suspended/blank rows must never overwrite a good stored close
                if close is None or close <= 0:
                    continue
                out[sym] = {
                    "date": d.strftime("%d-%b-%Y"),
                    "open": _num(r.get("OPEN_PRICE")),
                    "high": _num(r.get("HIGH_PRICE")),
                    "low": _num(r.get("LOW_PRICE")),
                    "close": close,
                    "volume": _num(r.get("TTL_TRD_QNTY")),
                }
    except Exception:
        pass
    return out


def build_bhavcopy_index(dates: list[date]) -> dict[str, dict[str, dict]]:
    """{date: {symbol: row}} - parse each cached daily file exactly once."""
    index: dict[str, dict[str, dict]] = {}
    for d in dates:
        parsed = _parse_bhavcopy_day(d)
        if parsed:
            index[d.strftime("%Y-%m-%d")] = parsed
    return index


def _bhav_series(index: dict[str, dict[str, dict]], symbol: str) -> list[dict]:
    points = []
    for key, day_map in index.items():
        row = day_map.get(symbol)
        if row:
            points.append(dict(row))
    return points


# --------------------------------------------------------------------------
# Yahoo Finance (pre-2020 / coverage-gap fallback)
# --------------------------------------------------------------------------
def _yahoo_session():
    opener = make_opener(cookies=True)
    try:
        http_get("https://fc.yahoo.com", timeout=12, opener=opener, retries=1)
    except Exception:
        pass
    try:
        crumb = http_get(YAHOO_CRUMB_URL, timeout=12, opener=opener, retries=1).decode("utf-8", "replace").strip()
    except Exception:
        crumb = ""
    return opener, crumb


def _fetch_yahoo(symbol: str, frm: date, tod: date, session=None) -> list[dict] | None:
    try:
        if session:
            opener, crumb = session
        else:
            opener, crumb = _yahoo_session()
        url = YAHOO_CHART_URL.format(sym=f"{symbol}.NS",
                                     p1=int(datetime.combine(frm, datetime.min.time()).timestamp()),
                                     p2=int(datetime.combine(tod, datetime.min.time()).timestamp()),
                                     crumb=crumb)
        raw = http_get(url, timeout=30, opener=opener, retries=2)
        result = json.loads(raw.decode("utf-8", "replace"))["chart"]["result"][0]
        ts = result.get("timestamp") or []
        quote = (result.get("indicators") or {}).get("quote") or [{}]
        closes = quote[0].get("close") or []
        points = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            # Yahoo epochs are exchange-local opens; convert via UTC explicitly
            # so a non-IST server cannot shift dates by a day.
            d = datetime.fromtimestamp(t, tz=timezone.utc)
            points.append({"date": d.strftime("%d-%b-%Y"), "close": float(c)})
        return _dedupe_sort(points) or None
    except Exception:
        return None


def _fetch_google(symbol: str) -> list[dict] | None:
    try:
        raw = http_get(GOOGLE_QUOTE_URL.format(sym=symbol), timeout=30, retries=1)
        text = raw.decode("utf-8", "replace")
        m = re.search(r'data-last-price="([\d.]+)"', text) or re.search(r'"l":\["([\d.]+)"', text)
        if not m:
            return None
        return [{"date": date.today().strftime("%d-%b-%Y"), "close": float(m.group(1))}]
    except Exception:
        return None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _load_history(isin: str) -> dict:
    return load_json(HISTORY_DIR / f"{isin}.json", {})


def _save_history(isin: str, doc: dict) -> None:
    save_json(HISTORY_DIR / f"{isin}.json", doc)


def _split_events(isin: str) -> list[tuple[str, float]]:
    """[(split ex-date 'YYYY-MM-DD', price factor)] from the actions file.

    A '2:1' split (numerator:denominator) halves the raw pre-split price, so
    historical RAW points must be multiplied by denominator/numerator to match
    Yahoo's split-adjusted scale."""
    doc = load_json(ACTIONS_DIR / f"{isin}.json", {}) or {}
    out: list[tuple[str, float]] = []
    for s in doc.get("splits") or []:
        try:
            num_s, den_s = str(s.get("ratio") or "").split(":")
            num, den = float(num_s), float(den_s)
        except ValueError:
            continue
        if num <= 0 or den <= 0:
            continue
        dk = date_key(norm_date(str(s.get("date") or "")))
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", dk or "")
        if not m:
            continue
        out.append((f"{m.group(1)}-{m.group(2)}-{m.group(3)}", den / num))
    out.sort()
    return out


def _apply_split_adjustments(isin: str, points: list[dict], watermark: str | None
                             ) -> tuple[list[dict], str]:
    """Scale raw bhavcopy-era points for splits not yet applied.

    Idempotent: ``watermark`` ('splits_applied_through') records the last split
    date already baked into the stored series, so repeated refreshes never
    double-adjust. Only points on/after BHAVCOPY_START are touched - earlier
    points come from Yahoo and are already adjusted.
    """
    splits = _split_events(isin)
    if not splits:
        return points, (watermark or "")
    pending = [s for s in splits if s[0] > (watermark or "")]
    if pending:
        for p in points:
            pk_m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_key(p.get("date", "") or ""))
            if not pk_m:
                continue
            pk = f"{pk_m.group(1)}-{pk_m.group(2)}-{pk_m.group(3)}"
            try:
                if datetime.strptime(pk, "%Y-%m-%d").date() < BHAVCOPY_START:
                    continue  # Yahoo-era point: already adjusted
            except ValueError:
                continue
            factor = 1.0
            for sd, f in pending:
                if pk < sd:
                    factor *= f
            if factor == 1.0:
                continue
            for k in ("open", "high", "low", "close"):
                v = p.get(k)
                if isinstance(v, (int, float)) and v:
                    p[k] = round(v * factor, 6)
    return points, splits[-1][0]


def refresh_stock(isin: str, ident: dict, daily: bool, session=None,
                  bhav_index: dict | None = None, since: date | None = None) -> dict:
    symbol = ident.get("symbol") or ""
    name = ident.get("name") or ""
    doc = _load_history(isin)
    existing = doc.get("history") or []

    # 1. Manual CSV (authoritative when present).
    manual = _load_manual_csv(isin, symbol)
    if manual:
        merged = _dedupe_sort(manual)
        doc = {**doc, "isin": isin, "symbol": symbol, "name": name,
               "currency": "INR", "source": "manual",
               "fetched_at": now_iso(), "history": merged}
        _save_history(isin, doc)
        return {"isin": isin, "symbol": symbol, "status": "ok", "points": len(merged)}

    # Split normalization: raw bhavcopy closes are put on the same
    # split-adjusted scale as Yahoo before merging (idempotent via watermark).
    existing, wm = _apply_split_adjustments(
        isin, [dict(p) for p in existing], doc.get("splits_applied_through"))
    doc["splits_applied_through"] = wm

    # 2. NSE bhavcopy (primary). For a full backfill the index covers 2020+;
    #    dates before that are kept from the existing (Yahoo) history.
    if bhav_index is not None:
        bhav = _bhav_series(bhav_index, symbol)
        if bhav:
            merged_map = {p["date"]: p for p in existing if p.get("close") is not None}
            for p in bhav:
                merged_map[p["date"]] = p  # bhavcopy is authoritative per date
            merged = _dedupe_sort(list(merged_map.values()))
            doc = {**doc, "isin": isin, "symbol": symbol, "name": name,
                   "currency": "INR", "source": "NSE bhavcopy",
                   "fetched_at": now_iso(), "history": merged}
            _save_history(isin, doc)
            return {"isin": isin, "symbol": symbol, "status": "ok", "points": len(merged)}

    # 3. Yahoo fallback (symbol absent from bhavcopy / pre-2020 gap).
    last_date = max((p["date"] for p in existing), default=None)
    frm = since or BHAVCOPY_START
    if last_date:
        try:
            frm = datetime.strptime(last_date, "%d-%b-%Y").date()
        except ValueError:
            pass
    yahoo = _fetch_yahoo(symbol, frm, date.today(), session=session) if symbol else None
    if yahoo:
        merged_map = {p["date"]: p for p in existing if p.get("close") is not None}
        for p in yahoo:
            merged_map[p["date"]] = p
        merged = _dedupe_sort(list(merged_map.values()))
        doc = {**doc, "isin": isin, "symbol": symbol, "name": name,
               "currency": "INR", "source": "Yahoo Finance",
               "fetched_at": now_iso(), "history": merged}
        _save_history(isin, doc)
        return {"isin": isin, "symbol": symbol, "status": "ok", "points": len(merged)}

    # 4. Google latest close (last resort).
    g = _fetch_google(symbol) if symbol else None
    if g:
        merged = _dedupe_sort(existing + g)
        doc = {**doc, "isin": isin, "symbol": symbol, "name": name,
               "currency": "INR", "source": "Google Finance",
               "fetched_at": now_iso(), "history": merged}
        _save_history(isin, doc)
        return {"isin": isin, "symbol": symbol, "status": "ok", "points": len(merged)}

    if existing:
        return {"isin": isin, "symbol": symbol, "status": "ok", "points": len(existing)}
    return {"isin": isin, "symbol": symbol, "status": "no_data"}


def run(ident: dict | None = None, symbols: list[str] | None = None,
        daily: bool = False, limit: int | None = None,
        workers: int = 10) -> list[dict]:
    ident = ident or load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    if limit:
        target = dict(list(target.items())[:limit])

    # Download the daily bhavcopy files ONCE (parallel agents), then reuse the
    # index for every stock.
    if daily:
        bhav_start = date.today() - timedelta(days=15)
        bhav_index = build_bhavcopy_index(download_bhavcopy_range(bhav_start, date.today(), workers))
    else:
        bhav_index = build_bhavcopy_index(download_bhavcopy_range(BHAVCOPY_START, date.today(), workers))

    session = _yahoo_session()  # one Yahoo session + crumb reused for the run
    out = []
    for i, (isin, ident_row) in enumerate(target.items(), 1):
        out.append(refresh_stock(isin, ident_row, daily=daily, session=session,
                                 bhav_index=bhav_index))
        if i % 25 == 0:
            print(f"  [{i}/{len(target)}] {isin}", flush=True)
        time.sleep(0.05)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="comma-separated NSE symbols to update")
    parser.add_argument("--daily", action="store_true", help="incremental (recent) update only")
    parser.add_argument("--limit", type=int, default=None, help="max stocks to process")
    parser.add_argument("--workers", type=int, default=10,
                        help="parallel bhavcopy download agents")
    args = parser.parse_args()
    ident = load_identity()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    results = run(ident=ident, symbols=symbols, daily=args.daily,
                  limit=args.limit, workers=args.workers)
    ok = sum(1 for r in results if r.get("status") == "ok")
    no = sum(1 for r in results if r.get("status") == "no_data")
    print(json.dumps({"total": len(results), "ok": ok, "no_data": no}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())