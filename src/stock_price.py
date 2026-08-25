"""Stock daily closing-price history agent (pure-NSE pipeline).

Produces ``data/stock_history/<ISIN>.json`` = ``{isin, symbol, name, currency,
source, fetched_at, history: [{date, close, open, high, low, volume}]}``.

[PLAN_STOCK_DATA_NSE_CLEANUP] De-Yahoo'd: every stored point comes from an
official NSE source on its own raw scale — NO cross-source split-adjustment
stitching (the old Yahoo-era normalization was the root cause of phantom
+-50-100% jumps at segment boundaries). Total-return math stays downstream
(chatapp ``total_return_price`` adjusts on the fly from the actions file).

Fetch chain per stock (first success wins, later sources fill gaps):
1. **Manual CSV** - ``data/raw/stock_manual/<ISIN or SYMBOL>.csv``
   (``date,close`` or ``date,open,high,low,close,volume``). Authoritative when present.
2. **NSE bhavcopy archives (primary)** - one daily file covers every NSE-listed
   symbol (OHLC). Primary endpoint: ``sec_bhavdata_full`` CSV on
   ``archives.nseindia.com``; fallback: the UDiFF Common Bhavcopy ZIP on
   ``nsearchives.nseindia.com``. Coverage starts ~2020.
3. **NSE historical cm/equity API (pre-2020 depth)** - symbol-level JSON
   covering 1994+, fetched in paced 1-year chunks via a Chrome-impersonated
   session. Access is IP/geo-dependent (503s in some environments): when
   unavailable the series honestly starts at BHAVCOPY_START instead of
   mixing scales. Set ``STOCK_ALLOW_YAHOO=1`` ONLY as a documented
   last-resort to retain legacy deep history.
4. **Google Finance (daily incremental only)** - latest close as a fast
   top-up.
5. **Yahoo Finance (flag-gated last resort)** - requires
   ``STOCK_ALLOW_YAHOO=1``; kept solely for emergency recovery of symbols
   absent from every NSE source.

For a backfill, the daily bhavcopy files are downloaded ONCE and reused for all
stocks (``run()`` builds an in-memory ``{date: {symbol: row}}`` index), so 868
stocks cost ~1,700 file downloads in total rather than 868 per-symbol calls.

Run::

    python -m src.stock_price --download-only   # PLAN phase-0 raw download:
                                                # bhavcopy top-up; NO stock_history writes
    python -m src.stock_price --dump-nse-history [--symbols X,Y]
                                                # PLAN phase-0 raw download:
                                                # pre-2020 points -> data/raw/nse_historical/
    python -m src.stock_price                # backfill (bhavcopy primary)
    python -m src.stock_price --daily        # incremental append (daily)
    python -m src.stock_price --symbols ADANIENT,RELIANCE
    python -m src.stock_price --workers 12   # parallel bhavcopy downloaders
    python -m src.stock_price --rebackfill-nse [--symbols X,Y]
                                             # PLAN_STOCK_DATA_NSE_CLEANUP
                                             # phase-3 corruption eraser
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .stock_common import (ACTIONS_DIR, HISTORY_DIR, MANUAL_DIR, date_key, http_get,
                           load_json, make_opener, norm_date, now_iso, save_json)
from .stock_identity import load_identity

BHAVCOPY_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "stock_bhavcopy"
# Primary: per-symbol full bhavdata CSV (archives host).
BHAVCOPY_ARCHIVE_URL = ("https://archives.nseindia.com/products/content/"
                        "sec_bhavdata_full_{ddmmyyyy}.csv")
# Fallback (directive: NSE deprecated legacy formats w.e.f. Jul-2024; the
# official archive is the UDiFF Common Bhavcopy ZIP on the nsearchives host):
BHAVCOPY_UDIFF_URL = ("https://nsearchives.nseindia.com/content/cm/"
                      "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip")
# Earliest reliably available "sec_bhavdata_full" file.
BHAVCOPY_START = date(2020, 1, 1)

# NSE historical cm/equity API (pre-2020 depth; IP/geo-dependent availability).
NSE_HISTORICAL_URL = ("https://www.nseindia.com/api/historical/cm/equity"
                      "?symbol={sym}&series=%22EQ%22&from={frm}&to={to}")
NSE_HIST_CHUNK_DAYS = 365          # 1-year windows, paced
NSE_HIST_PACING_S = 1.5            # Akamai politeness between chunks

YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}"
                   "&period2={p2}&interval=1d&crumb={crumb}")
GOOGLE_QUOTE_URL = "https://www.google.com/finance/quote/{sym}:NSE"

REBACKFILL_STATUS = BHAVCOPY_CACHE_DIR / "nse_backfill_status.json"

# Phase-0 raw dumps [PLAN_STOCK_DATA_NSE_CLEANUP]: pre-2020 points land here as
# plain JSON, one file per symbol; extraction into stock_history/ happens later.
NSE_HIST_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "nse_historical"
NSE_HIST_STATUS = NSE_HIST_RAW_DIR / "_status.json"
NSE_HIST_START = date(1994, 1, 1)
HIST_END = BHAVCOPY_START - timedelta(days=1)


def yahoo_allowed() -> bool:
    """Yahoo is a FLAG-GATED last resort [PLAN_STOCK_DATA_NSE_CLEANUP]."""
    return os.environ.get("STOCK_ALLOW_YAHOO", "").strip() == "1"


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
    """Fetch + cache one daily bhavcopy file. Returns the date on success.

    Chain: sec_bhavdata_full CSV (archives host) -> UDiFF Common Bhavcopy ZIP
    (nsearchives host). The UDiFF zip is extracted and cached as-is; the parser
    sniffs the format from the header row."""
    path = _bhav_path(d)
    if path.exists():
        # Reject cached HTML/PDF responses.
        try:
            raw = path.read_bytes()
            if raw[:6].startswith(b"SYMBOL") or raw[:6].startswith(b"TradDt"):
                return d
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
    data = None
    try:
        data = http_get(BHAVCOPY_ARCHIVE_URL.format(ddmmyyyy=d.strftime("%d%m%Y")),
                        timeout=30, retries=2)
        if not data[:6].startswith(b"SYMBOL"):
            data = None
    except Exception:
        data = None
    if data is None:
        try:
            zipped = http_get(BHAVCOPY_UDIFF_URL.format(
                yyyymmdd=d.strftime("%Y%m%d")), timeout=40, retries=2)
            with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
                inner = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
                data = zf.read(inner)
            if not data[:6].startswith(b"TradDt"):
                data = None
        except Exception:
            data = None
    if data is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return d


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
    """Parse a cached daily file -> {symbol: {open, high, low, close, volume}}.

    Supports both archive formats:
      - sec_bhavdata_full (SYMBOL/OPEN_PRICE/... headers)
      - UDiFF Common Bhavcopy (TradDt/TckrSymb/OpnPric/... headers)"""
    path = _bhav_path(d)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="latin-1", newline="") as fh:
            reader = csv.DictReader(fh)
            # NSE bhavcopy headers carry a leading space (' CLOSE_PRICE').
            reader.fieldnames = [c.strip() for c in (reader.fieldnames or [])]
            fields = set(reader.fieldnames or [])
            ud = "TradDt" in fields  # UDiFF Common Bhavcopy format
            for r in reader:
                sym = (r.get("TckrSymb" if ud else "SYMBOL") or "").strip()
                if not sym:
                    continue
                srs = (r.get("SctySrs" if ud else "SERIES") or "").strip()
                # equity series only; skip indices/ETF-units/debt rows AND
                # when-issued duplicates ('W1'/'W2'/'W3' during mergers) whose
                # prices are on a different share basis - e.g. HDFCBANK traded
                # ~1644 EQ alongside a phantom ~612 W3 row in Jul/Aug-2023.
                if srs and srs not in {"EQ", "BE", "BZ", "SM", "ST", "SZ"}:
                    continue
                close = _num(r.get("ClsPric" if ud else "CLOSE_PRICE"))
                # suspended/blank rows must never overwrite a good stored close
                if close is None or close <= 0:
                    continue
                out[sym] = {
                    "date": d.strftime("%d-%b-%Y"),
                    "open": _num(r.get("OpnPric" if ud else "OPEN_PRICE")),
                    "high": _num(r.get("HghPric" if ud else "HIGH_PRICE")),
                    "low": _num(r.get("LwPric" if ud else "LOW_PRICE")),
                    "close": close,
                    "volume": _num(r.get("TtlTradgVol" if ud else "TTL_TRD_QNTY")),
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
# NSE historical cm/equity API (pre-2020 depth; availability varies by env)
# --------------------------------------------------------------------------
class _NseHistoricalSession:
    """Chrome-impersonated session for www.nseindia.com JSON APIs.

    The plain urllib opener is fingerprint-blocked on some endpoints; the
    repo already ships curl_cffi for exactly this class of host
    (pdf_downloader). Session + cookies are created lazily and reused."""

    _session = None

    @classmethod
    def session(cls):
        if cls._session is None:
            from curl_cffi import requests as cffi
            s = cffi.Session(impersonate="chrome124")
            try:
                r = s.get("https://www.nseindia.com/", timeout=30)
                if r.status_code != 200:
                    raise RuntimeError(f"warm {r.status_code}")
            except Exception:
                pass                      # cookies may still be set
            cls._session = s
        return cls._session


def _nse_historical_chunk(symbol: str, frm: date, tod: date) -> list[dict]:
    """One [frm, tod] window from the historical API -> raw points.

    Never raises: any failure returns [] so callers degrade to bhavcopy-only
    coverage instead of crashing a multi-hour backfill."""
    url = NSE_HISTORICAL_URL.format(
        sym=symbol, frm=frm.strftime("%d-%m-%Y"), to=tod.strftime("%d-%m-%Y"))
    try:
        s = _NseHistoricalSession.session()
        r = s.get(url, headers={
            "Referer": "https://www.nseindia.com/market-data/"
                       "historical-data-equity-index",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=60)
        if r.status_code != 200:
            return []
        data = json.loads(r.content.decode("utf-8", "replace"))
    except Exception:
        return []
    rows = data.get("data") if isinstance(data, dict) else None
    out: list[dict] = []
    for row in rows or []:
        # field names observed: chTrdPrices / CH_TRADE_HIGH_PRICE etc.
        d_s = str(row.get("chTimestamp") or row.get("timestamp")
                  or row.get("date") or "")
        close = _num(row.get("chClosingPrice") or row.get("closePrice")
                     or row.get("close"))
        if not d_s or close is None or close <= 0:
            continue
        d = norm_date(d_s[:11]) or ""
        if not re.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", d):
            continue
        out.append({
            "date": d,
            "open": _num(row.get("chOpeningPrice") or row.get("openPrice")),
            "high": _num(row.get("chHighPrice") or row.get("highPrice")),
            "low": _num(row.get("chLowPrice") or row.get("lowPrice")),
            "close": close,
            "volume": _num(row.get("chTotTradedQty") or row.get("totalTradedQuantity")),
        })
    return out


def fetch_nse_historical(symbol: str, start: date, end: date,
                         pacing_s: float = NSE_HIST_PACING_S) -> list[dict] | None:
    """Full history in paced 1-year chunks. Returns None when the endpoint
    itself is unavailable (caller keeps whatever NSE coverage exists);
    [] when reachable but empty."""
    if not symbol or end < start:
        return [] if symbol else None
    chunks: list[tuple[date, date]] = []
    cur = end
    while cur >= start:
        c_from = max(start, cur - timedelta(days=NSE_HIST_CHUNK_DAYS - 1))
        chunks.append((c_from, cur))
        cur = c_from - timedelta(days=1)

    ok_any = False
    points: list[dict] = []
    for c_from, c_to in reversed(chunks):          # oldest first
        got = _nse_historical_chunk(symbol, c_from, c_to)
        if got:
            ok_any = True
            points.extend(got)
        time.sleep(pacing_s)                       # Akamai politeness
    if not ok_any:
        return None                                # endpoint unavailable
    return _dedupe_sort(points)


def download_only(workers: int = 10) -> dict:
    """Phase-0 [PLAN_STOCK_DATA_NSE_CLEANUP]: fill the raw-data folders only.

    Bhavcopy range top-up into ``data/stock_bhavcopy/``; no refresh_stock()
    calls — nothing is written into ``stock_history/`` here (fill is the LAST
    action of the pipeline)."""
    got = download_bhavcopy_range(BHAVCOPY_START, date.today(), workers)
    cached = sum(1 for d in trading_days(BHAVCOPY_START, date.today())
                 if _bhav_path(d).exists())
    return {"range": f"{BHAVCOPY_START.isoformat()}..{date.today().isoformat()}",
            "fetched_this_run": len(got), "files_cached": cached}


def _nse_hist_status() -> dict:
    return load_json(NSE_HIST_STATUS, {}) or {}


def dump_nse_history(symbols: list[str] | None = None,
                     pacing_s: float = NSE_HIST_PACING_S) -> dict:
    """Phase-0 [PLAN_STOCK_DATA_NSE_CLEANUP]: pre-2020 depth -> raw JSON dumps.

    Writes ``data/raw/nse_historical/<SYMBOL>.json`` per symbol + a resumable
    checkpoint at ``_status.json``. Never touches ``stock_history/``. When the
    endpoint proves unavailable the run stops immediately instead of pacing
    through every remaining symbol against a blocked host."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    status = _nse_hist_status()
    NSE_HIST_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = {"fetched": 0, "skipped": 0, "empty": 0, "unavailable": False,
           "total": len(target)}
    for n, (isin, row) in enumerate(target.items(), 1):
        symbol = row.get("symbol") or ""
        if not symbol or (status.get(symbol) or {}).get("status") in {"ok", "empty"}:
            out["skipped"] += 1
            continue
        points = fetch_nse_historical(symbol, NSE_HIST_START, HIST_END, pacing_s)
        if points is None:
            out["unavailable"] = True
            status[symbol] = {"status": "unavailable", "checked_at": now_iso()}
            save_json(NSE_HIST_STATUS, status)
            break
        doc = {"symbol": symbol, "isin": isin,
               "source": "NSE historical cm/equity", "fetched_at": now_iso(),
               "history": points}
        save_json(NSE_HIST_RAW_DIR / f"{symbol}.json", doc)
        status[symbol] = {"status": "empty" if not points else "ok",
                          "points": len(points), "dumped_at": now_iso()}
        save_json(NSE_HIST_STATUS, status)
        out["empty" if not points else "fetched"] += 1
        if n % 25 == 0:
            print(f"  [{n}/{len(target)}] {symbol}", flush=True)
        time.sleep(pacing_s / 2)
    return out


# --------------------------------------------------------------------------
# PLAN_STOCK_DATA_NSE_CLEANUP phase-3: the corruption eraser (local-only fill)
# --------------------------------------------------------------------------
def _load_nse_hist_dump(symbol: str) -> list[dict]:
    """Pre-2020 raw points from the phase-0 dump
    ``data/raw/nse_historical/<SYMBOL>.json`` ({symbol, history:[points]}).
    [] when absent (dump folder may still be empty - geo-blocked endpoint)."""
    if not symbol:
        return []
    doc = load_json(NSE_HIST_RAW_DIR / f"{symbol}.json", {}) or {}
    out: list[dict] = []
    for p in doc.get("history") or []:
        d = norm_date(str(p.get("date") or ""))
        close = _num(p.get("close"))
        if not re.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", d) or close is None or close <= 0:
            continue
        out.append({"date": d,
                    "open": _num(p.get("open")),
                    "high": _num(p.get("high")),
                    "low": _num(p.get("low")),
                    "close": close,
                    "volume": _num(p.get("volume"))})
    return out


def rebackfill_nse(symbols: list[str] | None = None, workers: int = 10,
                   limit: int | None = None, force: bool = False) -> dict:
    """Rebuild ``data/stock_history/<ISIN>.json`` purely from LOCAL downloads.

    Erases the Yahoo-stitched scale corruption at its root: the old chain
    merged raw bhavcopy points onto Yahoo-adjusted history (with
    ``_apply_split_adjustments`` scaling between them), producing phantom
    +-50-100% jumps at segment boundaries. Here each ISIN is instead:

      1. refilled from the two local raw sources only - pre-2020 depth from
         ``data/raw/nse_historical/<SYMBOL>.json`` (1994->2019, when the
         phase-0 dump exists) + 2020->today from the cached daily bhavcopy
         files (missing cache dates are simply skipped);
      2. merged with bhavcopy authoritative on overlap, deduped+sorted via
         ``_dedupe_sort``;
      3. kept on the RAW scale - no split adjustment, no cross-source
         normalization (total-return math stays downstream in chatapp);
      4. wipe-and-written as a fresh document (the legacy
         ``splits_applied_through`` watermark is gone);
      5. checkpointed into ``REBACKFILL_STATUS`` so a multi-hour run is
         resumable/idempotent: 'ok'/'no_source' ISINs are skipped on resume
         unless ``force``; 'failed' is always retried.

    Safety: when BOTH local segments come up empty the existing file is left
    UNTOUCHED and the ISIN is marked 'no_source' - never wipe without a
    replacement. No network calls happen during the fill itself; refresh the
    bhavcopy cache beforehand with ``--download-only`` if needed."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    if limit:
        target = dict(list(target.items())[:limit])

    # Local-only index: parse whatever daily files are already cached.
    dates = [d for d in trading_days(BHAVCOPY_START, date.today())
             if _bhav_path(d).exists()]
    bhav_index = build_bhavcopy_index(dates)

    status = load_json(REBACKFILL_STATUS, {}) or {}
    out = {"total": len(target), "ok": 0, "no_source": 0, "failed": 0,
           "skipped_resume": 0, "bhavcopy_dates": len(dates),
           "checkpoint": str(REBACKFILL_STATUS)}
    for n, (isin, row) in enumerate(target.items(), 1):
        symbol = row.get("symbol") or ""
        prev = status.get(isin) or {}
        if not force and prev.get("status") in {"ok", "no_source"}:
            out["skipped_resume"] += 1
            continue
        try:
            hist_seg = _load_nse_hist_dump(symbol)          # 1994 -> 2019
            bhav_seg = _bhav_series(bhav_index, symbol)     # 2020 -> today
            # bhavcopy listed FIRST => setdefault keeps it on overlap dates.
            merged = _dedupe_sort(bhav_seg + hist_seg)
            if merged:
                doc = {"isin": isin, "symbol": symbol,
                       "name": row.get("name") or "", "currency": "INR",
                       "source": "NSE local re-backfill",
                       "fetched_at": now_iso(), "history": merged}
                save_json(HISTORY_DIR / f"{isin}.json", doc)
                status[isin] = {"status": "ok", "points": len(merged),
                                "symbol": symbol, "refilled_at": now_iso()}
                out["ok"] += 1
            else:
                status[isin] = {"status": "no_source", "points": 0,
                                "symbol": symbol, "refilled_at": now_iso()}
                out["no_source"] += 1
        except Exception as e:
            status[isin] = {"status": "failed", "points": 0, "symbol": symbol,
                            "error": str(e)[:200], "refilled_at": now_iso()}
            out["failed"] += 1
        save_json(REBACKFILL_STATUS, status)
        if n % 25 == 0:
            print(f"  [{n}/{len(target)}] {symbol}", flush=True)
    return out


# --------------------------------------------------------------------------
# Yahoo Finance (FLAG-GATED last resort — see yahoo_allowed())
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
        # [BUG-L10] NSE quotes ≥ 1000 carry Indian commas ("1,234.56") — the
        # old [\d.]+ pattern silently skipped every high-priced stock.
        m = re.search(r'data-last-price="([\d,.]+)"', text) \
            or re.search(r'"l":\["([\d,.]+)"', text)
        if not m:
            return None
        return [{"date": date.today().strftime("%d-%b-%Y"),
                 "close": float(m.group(1).replace(",", ""))}]
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

    # 3. Google latest close (fast incremental top-up; runs BEFORE the slower
    #    Yahoo range call). In daily mode this completes today's series and we
    #    return; in full-backfill mode Google is skipped because a single point
    #    cannot fill history ranges — Yahoo handles those next.
    if daily:
        g = _fetch_google(symbol) if symbol else None
        if g:
            merged_map = {p["date"]: p for p in existing if p.get("close") is not None}
            for p in g:
                merged_map[p["date"]] = p
            merged = _dedupe_sort(list(merged_map.values()))
            doc = {**doc, "isin": isin, "symbol": symbol, "name": name,
                   "currency": "INR", "source": "Google Finance",
                   "fetched_at": now_iso(), "history": merged}
            _save_history(isin, doc)
            return {"isin": isin, "symbol": symbol, "status": "ok",
                    "points": len(merged), "source_used": "google"}

    # 4. Yahoo fallback (history/gap filler: pre-2020 coverage, symbols absent
    #    from bhavcopy, or non-daily runs needing ranged data).
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
    parser.add_argument("--download-only", action="store_true",
                        help="Phase-0 raw download: bhavcopy top-up only; no stock_history writes")
    parser.add_argument("--dump-nse-history", action="store_true",
                        help="Phase-0 raw download: pre-2020 points -> data/raw/nse_historical/<SYMBOL>.json")
    parser.add_argument("--rebackfill-nse", action="store_true",
                        help="PLAN phase-3: rebuild stock_history from LOCAL downloads only "
                             "(bhavcopy + nse_historical dumps; corruption eraser)")
    parser.add_argument("--force", action="store_true",
                        help="--rebackfill-nse: redo ISINs already marked ok/no_source in the checkpoint")
    args = parser.parse_args()
    if args.download_only or args.dump_nse_history:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
        result = download_only(args.workers) if args.download_only \
            else dump_nse_history(symbols)
        print(json.dumps(result, indent=2))
        return 1 if isinstance(result, dict) and result.get("unavailable") else 0
    if args.rebackfill_nse:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
        result = rebackfill_nse(symbols=symbols, workers=args.workers,
                                limit=args.limit, force=args.force)
        print(json.dumps(result, indent=2))
        return 0
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