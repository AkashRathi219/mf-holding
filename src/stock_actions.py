"""Corporate-actions agent (dividends + splits) per stock.

Produces ``data/stock_actions/<ISIN>.json`` = ``{isin, symbol, name, fetched_at,
dividends: [{date, amount}], splits: [{date, ratio}]}``.

Sources
-------
1. **Yahoo Finance events** — ``v8/finance/chart/<SYMBOL>.NS?events=div,split``
   returns dated dividend amounts and split ratios (reliable, no auth).
2. **NSE corporate announcements** — filtered to dividend/bonus/split keywords as
   a supplementary view (``nseindia.com/api/corporate-announcements``).
3. **NSE corporate-actions filings API** [PLAN_STOCK_DATA_NSE_CLEANUP phase-2] —
   ``corporates-corporateActions?index=equities&symbol=<SYM>`` returns the latest
   ~20 structured rows (subject/exDate/recDate/faceVal); dumped RAW by
   ``--dump-nse-actions`` into ``data/raw/nse_actions/`` — extraction into
   ``stock_actions/`` happens later (fill-last policy).

Run::

    python -m src.stock_actions                 # all resolved stocks
    python -m src.stock_actions --symbols ADANIENT
    python -m src.stock_actions --dump-nse-actions [--symbols X,Y]
                                                # PLAN phase-0 raw download only
    python -m src.stock_actions --fill-from-dumps [--symbols X,Y] [--dry-run]
                                                # PLAN phase-2 raw dumps -> store
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .stock_common import (ACTIONS_DIR, date_key, http_get, load_json, norm_date,
                           nse_session, now_iso, save_json)
from .stock_identity import load_identity

YAHOO_EVENTS_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}?"
                    "range=max&interval=1d&events=div%2Csplit")
NSE_ANNOUNCE_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={sym}")
NSE_CORP_ACTIONS_URL = ("https://www.nseindia.com/api/corporates-corporateActions"
                        "?index=equities&symbol={sym}")

# Phase-0 raw dumps [PLAN_STOCK_DATA_NSE_CLEANUP]; fill happens last.
ACTIONS_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "nse_actions"
ACTIONS_RAW_STATUS = ACTIONS_RAW_DIR / "_status.json"
# Last-resort dumps (Yahoo events) live SEPARATELY so nse_actions/ stays
# pure-NSE; merge policy decides at fill time.
YAHOO_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "yahoo_actions"
YAHOO_RAW_STATUS = YAHOO_RAW_DIR / "_status.json"

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


def fetch_nse_corporate_actions(symbol: str) -> list[dict] | None:
    """Latest (<=20) structured corporate-action rows for one symbol.

    Rows carry {symbol, isin, subject, exDate, recDate, bcStartDate/bcEndDate,
    faceVal, series}; the dividend amount / split ratio lives in ``subject``.
    Returns None only when the endpoint call fails (vs [] when reachable but
    empty). No date-range or pagination params exist — the feed is always the
    most recent window."""
    try:
        raw = http_get(NSE_CORP_ACTIONS_URL.format(sym=symbol),
                       headers={"Referer": "https://www.nseindia.com/"
                                           "companies-listing/corporate-filings-actions",
                                "Accept": "application/json, text/plain, */*"},
                       timeout=20, opener=nse_session(), retries=1)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, list) else []


def dump_nse_actions(symbols: list[str] | None = None,
                     pacing_s: float = 1.2) -> dict:
    """Phase-0 [PLAN_STOCK_DATA_NSE_CLEANUP]: corporate actions -> raw dumps.

    Writes ``data/raw/nse_actions/<SYMBOL>.json`` per symbol + resumable
    checkpoint at ``_status.json``. Never touches ``data/stock_actions/`` —
    extraction into JSON is a later, separate action. A failure streak triggers
    a cool-down (transient Akamai rate-limits recover in <1 min); the run only
    aborts after COOLDOWNS consecutive unrecovered streaks. Re-run resumes from
    the checkpoint."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    status = load_json(ACTIONS_RAW_STATUS, {}) or {}
    ACTIONS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = {"fetched": 0, "skipped": 0, "empty": 0, "failed": 0,
           "total": len(target)}
    misses = 0
    cooldowns = 0
    for n, (isin, row) in enumerate(target.items(), 1):
        symbol = row.get("symbol") or ""
        if not symbol or (status.get(symbol) or {}).get("status") in {"ok", "empty"}:
            out["skipped"] += 1
            continue
        rows = fetch_nse_corporate_actions(symbol)
        if rows is None:
            out["failed"] += 1
            misses += 1
            status[symbol] = {"status": "failed", "checked_at": now_iso()}
            save_json(ACTIONS_RAW_STATUS, status)
            if misses >= 10:
                cooldowns += 1
                if cooldowns > 3:
                    break
                print(f"  [cooldown {cooldowns}] after {misses} misses; "
                      f"sleeping 90s", flush=True)
                time.sleep(90)
                misses = 0
            else:
                time.sleep(pacing_s * 2)
            continue
        misses = 0
        doc = {"symbol": symbol, "isin": isin,
               "source": "NSE corporates-corporateActions",
               "fetched_at": now_iso(), "actions": rows}
        save_json(ACTIONS_RAW_DIR / f"{symbol}.json", doc)
        status[symbol] = {"status": "empty" if not rows else "ok",
                          "rows": len(rows), "dumped_at": now_iso()}
        save_json(ACTIONS_RAW_STATUS, status)
        out["empty" if not rows else "fetched"] += 1
        if n % 50 == 0:
            print(f"  [{n}/{len(target)}] {symbol}", flush=True)
        time.sleep(pacing_s)
    return out


def _fetch_nse_announcement_hits(symbol: str) -> list[dict] | None:
    """Full-history corporate announcements matching dividend/split keywords.

    Unlike :func:`_fetch_nse_announcements` this is untruncated (the feed holds
    years of rows per symbol) and returns None on call failure so callers can
    distinguish blocked-vs-empty."""
    try:
        raw = http_get(NSE_ANNOUNCE_URL.format(sym=symbol),
                       headers={"Referer": "https://www.nseindia.com/"
                                           "companies-listing/corporate-filings-announcements",
                                "Accept": "application/json, text/plain, */*"},
                       timeout=30, opener=nse_session(), retries=1)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    hits = []
    for a in data or []:
        text = f"{a.get('attchmntText') or ''} {a.get('an_subject') or ''}"
        if _DIV_KEYWORDS.search(text):
            hits.append({"date": (a.get("an_dt") or "")[:11],
                         "headline": text.strip()[:200],
                         "url": a.get("attchmntFile") or "",
                         "size": a.get("attFileSize") or ""})
    return hits


def backfill_empty_from_announcements(symbols: list[str] | None = None,
                                      pacing_s: float = 1.2) -> dict:
    """Phase-0 supplement [PLAN_STOCK_DATA_NSE_CLEANUP]: fill empty windows.

    For every symbol whose structured corp-actions dump came back empty, pull
    the full-history ``corporate-announcements`` feed and store the dividend/
    split/bonus keyword hits (each carries its PDF attachment URL for the
    later AI-extraction step). Existing structured rows are preserved; the
    checkpoint gains status ``ok_via_announcements``."""
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    status = load_json(ACTIONS_RAW_STATUS, {}) or {}
    ACTIONS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = {"backfilled": 0, "skipped": 0, "still_empty": 0, "failed": 0,
           "total": len(target)}
    misses = 0
    cooldowns = 0
    for n, (isin, row) in enumerate(target.items(), 1):
        symbol = row.get("symbol") or ""
        prev_status = (status.get(symbol) or {}).get("status")
        cur = load_json(ACTIONS_RAW_DIR / f"{symbol}.json", {}) or {}
        if not symbol or prev_status != "empty" or cur.get("actions"):
            out["skipped"] += 1
            continue
        hits = _fetch_nse_announcement_hits(symbol)
        if hits is None:
            out["failed"] += 1
            misses += 1
            if misses >= 10:
                cooldowns += 1
                if cooldowns > 3:
                    break
                print(f"  [cooldown {cooldowns}] after {misses} misses; "
                      f"sleeping 90s", flush=True)
                time.sleep(90)
                misses = 0
            else:
                time.sleep(pacing_s * 2)
            continue
        misses = 0
        doc = {**cur, "isin": cur.get("isin") or isin,
               "source": cur.get("source") or "NSE corporates-corporateActions",
               "fetched_at": now_iso(),
               "actions": cur.get("actions") or [],
               "announcements": hits}
        save_json(ACTIONS_RAW_DIR / f"{symbol}.json", doc)
        status[symbol] = {"status": "ok_via_announcements" if hits else "empty",
                          "rows": len(hits), "dumped_at": now_iso()}
        save_json(ACTIONS_RAW_STATUS, status)
        out["still_empty" if not hits else "backfilled"] += 1
        if n % 25 == 0:
            print(f"  [{n}/{len(target)}] {symbol}", flush=True)
        time.sleep(pacing_s)
    return out


def backfill_empty_from_yahoo(symbols: list[str] | None = None,
                              pacing_s: float = 0.8) -> dict:
    """Last-resort [PLAN_STOCK_DATA_NSE_CLEANUP]: Yahoo events for empty NSE.

    Only symbols with NO structured actions AND no announcement hits are
    eligible. Requires ``STOCK_ALLOW_YAHOO=1`` (flag-gated per plan). Writes
    ``data/raw/yahoo_actions/<SYMBOL>.json`` — a separate folder, so
    ``data/raw/nse_actions/`` remains pure-NSE for the fill phase."""
    if not os.environ.get("STOCK_ALLOW_YAHOO", "").strip() == "1":
        return {"error": "STOCK_ALLOW_YAHOO=1 required (plan: flag-gated last resort)"}
    ident = load_identity()
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    nse_status = load_json(ACTIONS_RAW_STATUS, {}) or {}
    status = load_json(YAHOO_RAW_STATUS, {}) or {}
    YAHOO_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = {"fetched": 0, "skipped": 0, "still_empty": 0, "failed": 0,
           "total": len(target)}
    for n, (isin, row) in enumerate(target.items(), 1):
        symbol = row.get("symbol") or ""
        prev = (status.get(symbol) or {}).get("status")
        nse_doc = load_json(ACTIONS_RAW_DIR / f"{symbol}.json", {}) or {}
        eligible = (not symbol
                    or (nse_status.get(symbol) or {}).get("status") == "empty"
                    or not (nse_doc.get("actions") or nse_doc.get("announcements")))
        if not eligible or prev in {"ok", "empty"}:
            out["skipped"] += 1
            continue
        dividends, splits = _fetch_yahoo_events(symbol)
        doc = {"symbol": symbol, "isin": isin,
               "source": "Yahoo Finance events (last-resort)",
               "fetched_at": now_iso(),
               "dividends": dividends, "splits": splits}
        save_json(YAHOO_RAW_DIR / f"{symbol}.json", doc)
        status[symbol] = {"status": "empty" if not (dividends or splits) else "ok",
                          "dividends": len(dividends), "splits": len(splits),
                          "dumped_at": now_iso()}
        save_json(YAHOO_RAW_STATUS, status)
        out["still_empty" if not (dividends or splits) else "fetched"] += 1
        if n % 25 == 0:
            print(f"  [{n}/{len(target)}] {symbol}", flush=True)
        time.sleep(pacing_s)
    return out


# --------------------------------------------------------------------------
# Phase-2 fill [PLAN_STOCK_DATA_NSE_CLEANUP]: parse raw dumps -> store
# --------------------------------------------------------------------------
NSE_SOURCE = "NSE corporates-corporateActions"
YAHOO_FALLBACK_SOURCE = "Yahoo Finance events (last-resort)"

# Subject-line grammar (case/whitespace tolerant; corpus includes typos like
# 'Rs - 26 Per Share', 'Pr Share', 'Pershare', 'Rs 62..50/-', 'Frm', 'tors').
_SUBJ_DOT_NORM_RE = re.compile(r"\.{2,}")
_SUBJ_NUM_SPACE_RE = re.compile(r"(\d)\s*\.\s*(\d)")
_SUBJ_DIV_WORD_RE = re.compile(r"\bdiv\b|divid", re.I)
_SUBJ_AMT_PER_SHARE_RE = re.compile(
    r"(?:rs|re)?[-:\s.]*(\d+(?:\.\d+)?)\s*/?-?\s*:?\s*-?\s*"
    r"(?:p(?:er|r)?\s*(?:(?:equity|ordinary|ord)\s*)?)?sh\w*", re.I)
_SUBJ_INCLUDING_RE = re.compile(r"includ", re.I)
_SUBJ_BONUS_SKIP_RE = re.compile(r"debenture|\bdeb\d|preference|ncrps", re.I)
_SUBJ_BONUS_RE = re.compile(r"\bbon[a-z]*\s*[:-]?\s*(\d{1,3})(?!\d)\s*:\s*(\d{1,3})(?!\d)", re.I)
_SUBJ_FVSPLIT_KW_RE = re.compile(r"face value spl\w*|fv spl\w*|stock split|sub-divis", re.I)
_SUBJ_FV_FROM_RE = re.compile(r"\b(?:from|frm)\s*(?:rs|re)?[.\s]*(\d+(?:\.\d+)?)", re.I)
_SUBJ_FV_TO_RE = re.compile(r"\bto\s*(?:rs|re)?[.\s]*(\d+(?:\.\d+)?)", re.I)
_SUBJ_MONEY_RE = re.compile(r"(?:rs|re)[.\s]*(\d+(?:\.\d+)?)", re.I)
_BONUS_PART_MAX = 500
_DK_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _norm_subject(subject: str) -> str:
    """Fix the corpus's numeric typos: '62..50' -> '62.50', '0 .70' -> '0.70'."""
    s = _SUBJ_DOT_NORM_RE.sub(".", subject or "")
    return _SUBJ_NUM_SPACE_RE.sub(r"\1.\2", s).strip()


def _norm_ratio(text) -> str | None:
    """'2.0:1.0' / '10/2' style pair -> reduced 'N:D' (price x D/N), else None."""
    try:
        num_s, den_s = str(text).split(":")
        fr = Fraction(num_s.strip()) / Fraction(den_s.strip())
    except Exception:
        return None
    if fr.numerator <= 0 or fr.denominator <= 0:
        return None
    return f"{fr.numerator}:{fr.denominator}"


def parse_subject_events(subject: str) -> list[dict]:
    """Subject line -> ``[{'kind': 'dividend', 'amount': N} | {'kind': 'split',
    'ratio': 'N:D'}]``.

    Grammar handled (real examples from data/raw/nse_actions/):
      * ``Dividend - Rs 13 Per Share`` / ``Interim Dividend Rs.8/- Per Share`` /
        ``Agm/Div-Rs.5/- Per Share`` / ``... Per Equity|Ordinary Share`` /
        currency-less ``Interim Dividend - 14.50 Per Share``
        -> dividend (abs amount; percent-only subjects yield nothing).
        Dual declarations on one line (``Dividend X + Special Dividend Y``)
        SUM into the ex-date's total cash — unless phrased
        ``(Including Special Dividend ...)`` where X already covers Y.
      * ``Bonus A:B`` (equity only — debenture/preference/NCRPS bonuses skipped)
        -> split-equivalent ratio ``(A+B):B``, i.e. price x B/(A+B); a 1:1 bonus
        halves the price just like a 2-for-1 split.
      * ``Face Value Split (Sub-Division) From Rs 10/- To Re 1/-`` /
        ``Stock Split - From Rs 10 To Rs 5`` / typo'd ``Fv Splt Frm Rs 10 To Rs 2``
        -> ratio ``OLD:NEW`` (FV 10->2 == price /5 == each old share becomes
        5 new == '5:1'), consistent with stock_price._split_events.
      * Bonus + FV-split on one line compose into ONE ratio per ex-date.
    Anything else (rights/buyback/AGM/demerger/...) yields no events.
    """
    events: list[dict] = []
    s = _norm_subject(subject)
    if not s:
        return events
    if _SUBJ_DIV_WORD_RE.search(s):
        # Sum every per-share amount on the line: 'Interim X / Special Y'
        # rows declare both for the same ex-date. '(Including Special...)'
        # states an all-in figure, so keep just the first amount there.
        # FV-split denominators ('...Per Share') must NOT count as dividends,
        # so matches inside the split phrase's span are ignored.
        lo = hi = None
        mkw = _SUBJ_FVSPLIT_KW_RE.search(s)
        if mkw:
            ends = [m.end() for m in (_SUBJ_FV_FROM_RE.search(s),
                                      _SUBJ_FV_TO_RE.search(s)) if m]
            lo, hi = mkw.start(), (max(ends) if ends else len(s))
        amts = [float(m.group(1)) for m in _SUBJ_AMT_PER_SHARE_RE.finditer(s)
                if float(m.group(1)) > 0 and not (lo is not None and lo <= m.start() < hi)]
        if amts:
            total = amts[0] if _SUBJ_INCLUDING_RE.search(s) else sum(amts)
            events.append({"kind": "dividend", "amount": round(total, 4)})
    bonus_ab: tuple[int, int] | None = None
    if not _SUBJ_BONUS_SKIP_RE.search(s):
        mb = _SUBJ_BONUS_RE.search(s)
        if mb:
            a, b = int(mb.group(1)), int(mb.group(2))
            if 1 <= a <= _BONUS_PART_MAX and 1 <= b <= _BONUS_PART_MAX:
                bonus_ab = (a, b)
    fv_pair: tuple[Fraction, Fraction] | None = None
    if _SUBJ_FVSPLIT_KW_RE.search(s):
        mf, mt = _SUBJ_FV_FROM_RE.search(s), _SUBJ_FV_TO_RE.search(s)
        old = new = None
        if mf and mt:
            old, new = mf.group(1), mt.group(1)
        else:
            # Keyword-free/one-sided forms like 'Fv Split Rs.10/- To Rs.2/':
            # first two money tokens are old/new (sub-divisions go high -> low).
            toks = _SUBJ_MONEY_RE.findall(s)
            if len(toks) >= 2:
                old, new = toks[0], toks[1]
        try:
            fo, fn = Fraction(old), Fraction(new)
        except Exception:
            fo = fn = None
        if fo is not None and fn is not None and fo > 0 and fo > fn:
            fv_pair = (fo, fn)
    if bonus_ab or fv_pair:
        ratio = Fraction(1)
        if bonus_ab:
            a, b = bonus_ab
            ratio *= Fraction(a + b, b)   # shares x(A+B)/B => price xB/(A+B)
        if fv_pair:
            old, new = fv_pair
            ratio *= old / new            # FV sub-division => price xNEW/OLD
        events.append({"kind": "split",
                       "ratio": f"{ratio.numerator}:{ratio.denominator}"})
    return events


def _event_date_key(raw_date) -> str:
    """exDate ('DD-MMM-YYYY' or ISO) -> sortable 'YYYY-MM-DD' prefix key."""
    dk = date_key(norm_date(str(raw_date or "").strip()))
    return dk if _DK_PREFIX_RE.match(dk) else ""


def _events_from_rows(rows: list) -> tuple[dict[str, dict], dict[str, dict], int]:
    """Parse NSE dump rows -> ({date_key: dividend}, {date_key: split}, used).

    Rows without events (rights/buyback/AGM...) or without a usable exDate are
    ignored by the caller's skip accounting; last row per date wins (revised
    announcements overwrite their originals)."""
    dividends: dict[str, dict] = {}
    splits: dict[str, dict] = {}
    used = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        evs = parse_subject_events(str(r.get("subject") or ""))
        if not evs:
            continue
        dk = _event_date_key(r.get("exDate"))
        if not dk:
            continue
        used += 1
        display = norm_date(str(r.get("exDate") or "").strip())
        for ev in evs:
            if ev["kind"] == "dividend":
                dividends[dk] = {"date": display, "amount": ev["amount"]}
            else:
                splits[dk] = {"date": display, "ratio": ev["ratio"]}
    return dividends, splits, used


def _entry_date_key(entry: dict) -> str:
    return _event_date_key((entry or {}).get("date"))


def _same_event_value(a, b, field: str) -> bool:
    if field == "amount":
        try:
            return abs(float(a) - float(b)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return str(a or "") == str(b or "")


def _merge_kind(existing: list, overrides: dict[str, dict], field: str) -> tuple[list[dict], int, int]:
    """Apply same-date-key overrides over stored events, preserving the rest.

    Returns (merged_sorted_by_date, added, updated); ``updated`` counts only
    real value changes so a second fill run reports zeros (idempotency)."""
    merged: list[dict] = [dict(e) for e in existing if isinstance(e, dict)]
    idx: dict[str, int] = {}
    for pos, e in enumerate(merged):
        k = _entry_date_key(e)
        if k:
            idx.setdefault(k, pos)
    added = updated = 0
    for dk in sorted(overrides):
        pos = idx.get(dk)
        if pos is None:
            merged.append(dict(overrides[dk]))
            idx[dk] = len(merged) - 1
            added += 1
        elif not _same_event_value(merged[pos].get(field), overrides[dk][field], field):
            merged[pos] = {**merged[pos], field: overrides[dk][field]}
            updated += 1
    merged.sort(key=lambda e: _entry_date_key(e) or "9999-99-99")
    return merged, added, updated


def _yahoo_fallback_events(symbol: str, yahoo_raw_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Last-resort dumps {date, amount}/{date, ratio} keyed like NSE overrides."""
    doc = load_json(yahoo_raw_dir / f"{symbol}.json", {}) or {}
    dividends: dict[str, dict] = {}
    for e in doc.get("dividends") or []:
        dk = _event_date_key((e or {}).get("date"))
        if dk and isinstance(e, dict):
            try:
                dividends[dk] = {"date": norm_date(str(e["date"]).strip()),
                                 "amount": round(float(e.get("amount") or 0), 4)}
            except (TypeError, ValueError):
                continue
    splits: dict[str, dict] = {}
    for e in doc.get("splits") or []:
        dk = _event_date_key((e or {}).get("date"))
        ratio = _norm_ratio((e or {}).get("ratio"))
        if dk and ratio and isinstance(e, dict):
            splits[dk] = {"date": norm_date(str(e["date"]).strip()), "ratio": ratio}
    return dividends, splits


def _sorted_view(entries: list) -> list[dict]:
    return sorted((dict(e) for e in entries), key=lambda e: _entry_date_key(e) or "9999-99-99")


_SPLIT_DUPE_WINDOW_DAYS = 60


def _is_split_near_duplicate(new: dict, existing: list) -> bool:
    """True when ``existing`` already holds the SAME corporate split/bonus
    under the other feed's date convention (equal reduced ratio within the
    window). Yahoo dates NSE-reported bonuses/splits by days-weeks; storing
    both would make stock_price._split_events double-adjust the series."""
    ndk = _event_date_key(new.get("date"))
    ratio = _norm_ratio(new.get("ratio"))
    if not ndk or not ratio:
        return False
    try:
        ndate = datetime.strptime(ndk[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    for e in existing:
        if not isinstance(e, dict) or _norm_ratio(e.get("ratio")) != ratio:
            continue
        edk = _event_date_key(e.get("date"))
        try:
            if abs((ndate - datetime.strptime(edk[:10], "%Y-%m-%d").date()).days) \
                    <= _SPLIT_DUPE_WINDOW_DAYS:
                return True
        except ValueError:
            continue
    return False


def _fill_one_isin(isin: str, symbol: str, ident_row: dict, *, dry_run: bool,
                   actions_dir: Path, nse_raw_dir: Path, yahoo_raw_dir: Path
                   ) -> tuple[str, dict, bool]:
    """Merge one ISIN. Returns (status, deltas, yahoo_used).

    status: ok | unchanged | skipped (no usable dump) | aborted_shrink."""
    nse_doc = load_json(nse_raw_dir / f"{symbol}.json", {}) or {}
    nse_div, nse_split, _used = _events_from_rows(nse_doc.get("actions") or [])
    sources: list[str] = []
    if nse_doc:
        sources.append(NSE_SOURCE)
    yahoo_used = False
    if not (nse_div or nse_split):
        y_div, y_split = _yahoo_fallback_events(symbol, yahoo_raw_dir)
        if y_div or y_split:
            nse_div, nse_split = y_div, y_split
            yahoo_used = True
            sources.append(YAHOO_FALLBACK_SOURCE)
    if not (nse_div or nse_split):
        return "skipped", {}, yahoo_used
    prev = load_json(actions_dir / f"{isin}.json", {}) or {}
    before_div = prev.get("dividends") or []
    before_split = prev.get("splits") or []
    # Same-event-under-both-date-conventions guard: exact-date overrides still
    # apply, brand-new rows that merely re-report a known nearby split are
    # dropped instead of doubled (price backfill would multiply twice).
    known_dks = {_entry_date_key(e) for e in before_split if isinstance(e, dict)}
    nse_split = {dk: v for dk, v in nse_split.items()
                 if dk in known_dks or not _is_split_near_duplicate(v, before_split)}
    merged_div, d_add, d_upd = _merge_kind(before_div, nse_div, "amount")
    merged_split, s_add, s_upd = _merge_kind(before_split, nse_split, "ratio")
    # SAFETY: never shrink curated history — abort the ISIN instead of writing.
    if len(merged_div) + len(merged_split) < len(before_div) + len(before_split):
        return "aborted_shrink", {}, yahoo_used
    new_sources = list(prev.get("sources") or [])
    for src in sources:
        if src not in new_sources:
            new_sources.append(src)
    changed = (_sorted_view(merged_div) != _sorted_view(before_div)
               or _sorted_view(merged_split) != _sorted_view(before_split)
               or new_sources != list(prev.get("sources") or [])
               or not prev)
    if changed and not dry_run:
        doc = dict(prev)
        doc.update({"isin": prev.get("isin") or isin,
                    "symbol": prev.get("symbol") or symbol,
                    "name": prev.get("name") or (ident_row.get("name") or ""),
                    "fetched_at": now_iso(),
                    "dividends": merged_div, "splits": merged_split,
                    "sources": new_sources})
        doc.setdefault("announcements", [])
        save_json(actions_dir / f"{isin}.json", doc)
    status = "ok" if changed else "unchanged"
    return status, {"dividends_added": d_add, "dividends_updated": d_upd,
                    "splits_added": s_add, "splits_updated": s_upd}, yahoo_used


def fill_actions_from_dumps(symbols: list[str] | None = None,
                            identity: dict | None = None, dry_run: bool = False,
                            actions_dir: Path | None = None,
                            nse_raw_dir: Path | None = None,
                            yahoo_raw_dir: Path | None = None) -> dict:
    """Phase-2 fill [PLAN_STOCK_DATA_NSE_CLEANUP]: raw dumps -> store.

    For every ISIN the NSE corp-actions dump is parsed and merged OVER the
    existing ``data/stock_actions/<ISIN>.json`` window: same-date-key
    dividends/splits are overridden (value-changes counted as updates), all
    pre-NSE deep history is preserved. When the NSE window contributes zero
    events, the last-resort Yahoo dump (if any) merges under the same policy
    and the doc gains an additive ``sources`` note. Idempotent: docs whose
    content would not change are left untouched (fetched_at included).
    Honours --dry-run; refuses to shrink (abort + aborted_shrink count)."""
    ident = identity if identity is not None else load_identity()
    actions_dir = actions_dir or ACTIONS_DIR
    nse_raw_dir = nse_raw_dir or ACTIONS_RAW_DIR
    yahoo_raw_dir = yahoo_raw_dir or YAHOO_RAW_DIR
    if symbols:
        wanted = {s.upper() for s in symbols}
        target = {i: v for i, v in ident.items() if (v.get("symbol") or "") in wanted}
    else:
        target = {i: v for i, v in ident.items() if v.get("symbol")}
    out = {"processed": len(target), "dividends_added": 0, "dividends_updated": 0,
           "splits_added": 0, "splits_updated": 0, "yahoo_used": 0,
           "aborted_shrink": 0, "skipped": 0}
    for n, (isin, row) in enumerate(sorted(target.items(), key=lambda kv: kv[1].get("symbol") or ""), 1):
        symbol = row.get("symbol") or ""
        if not symbol:
            out["skipped"] += 1
            continue
        status, deltas, yahoo_used = _fill_one_isin(
            isin, symbol, row, dry_run=dry_run, actions_dir=actions_dir,
            nse_raw_dir=nse_raw_dir, yahoo_raw_dir=yahoo_raw_dir)
        if status == "skipped":
            out["skipped"] += 1
        elif status == "aborted_shrink":
            out["aborted_shrink"] += 1
        else:
            for k, v in deltas.items():
                out[k] += v
            if yahoo_used:
                out["yahoo_used"] += 1
        if n % 50 == 0:
            print(f"  [{n}/{len(target)}]", flush=True)
    return out


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
    parser.add_argument("--dump-nse-actions", action="store_true",
                        help="Phase-0 raw download only: NSE corporate actions -> data/raw/nse_actions/")
    parser.add_argument("--backfill-empty-actions", action="store_true",
                        help="Phase-0 supplement: announcements-feed hits for symbols with empty corp-actions windows")
    parser.add_argument("--backfill-empty-yahoo", action="store_true",
                        help="Last resort (STOCK_ALLOW_YAHOO=1): Yahoo events for NSE-empty symbols -> data/raw/yahoo_actions/")
    parser.add_argument("--fill-from-dumps", action="store_true",
                        help="Phase-2 fill: parse data/raw/nse_actions (+yahoo last-resort) -> data/stock_actions/<ISIN>.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --fill-from-dumps: report merge counts without writing")
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    if (args.dump_nse_actions or args.backfill_empty_actions
            or args.backfill_empty_yahoo or args.fill_from_dumps):
        result = dump_nse_actions(symbols) if args.dump_nse_actions \
            else backfill_empty_from_announcements(symbols) \
            if args.backfill_empty_actions else backfill_empty_from_yahoo(symbols) \
            if args.backfill_empty_yahoo else fill_actions_from_dumps(symbols, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    ident = load_identity()
    results = run(ident=ident, symbols=symbols, limit=args.limit)
    print(json.dumps(results[:10], indent=2))
    print(json.dumps({"total": len(results)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())