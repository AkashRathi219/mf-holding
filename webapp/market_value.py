"""Current-market-value pricing for portfolio items.

For every holding the investor's *current* market value is ``units x latest
price/NAV``. This module builds an ISIN -> {nav, date} index from the daily NAV
history (``data/nav_history``), the universe NAVs stored on the schemes table,
and (for direct stocks) the daily price history (``data/stock_history``), then
re-weights a portfolio so analysis uses market value instead of cost.

The index is cached to ``data/reference/isin_latest_nav.json`` and rebuilt only
when the NAV/price directories change, so it always reflects the latest data.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import remote_store

BASE_DIR = Path(__file__).resolve().parent.parent
NAV_DIR = BASE_DIR / "data" / "nav_history"
STOCK_DIR = BASE_DIR / "data" / "stock_history"
INDEX_PATH = BASE_DIR / "data" / "reference" / "isin_latest_nav.json"

_MAX_INDEX_AGE = 60 * 60 * 6  # rebuild at most every 6h, or when dirs change

# [BUG-F4] malformed NAV files (history = plain date strings, no NAV rows)
# used to raise AttributeError OUTSIDE the try block and kill the whole
# index rebuild -> reweight_by_market_value -> /analyze. Scanners now skip
# non-dict history rows/files and remember them for data-health.
_LAST_ANOMALIES: list[dict] = []

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso_date(d) -> str | None:
    """Normalise the many date formats to ISO (YYYY-MM-DD); None if unparseable."""
    key = _dtkey(d)
    if key == (0, 0, 0):
        return None
    return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"


def scheme_latest_nav(scheme: dict, prefer: str = "regular") -> tuple | None:
    """(nav, date) from the freshest of the scheme's own nav-history files
    (keyed by its AMFI codes). ``prefer`` resolves date ties in favour of the
    requested plan ('regular' / 'direct'). Falls back to the universe ``nav``."""
    nav_dir = BASE_DIR / "data" / "nav_history"
    if prefer == "direct":
        codes = (scheme.get("amfi_direct"), scheme.get("amfi_regular"))
    else:
        codes = (scheme.get("amfi_regular"), scheme.get("amfi_direct"))
    best, best_key = None, (0, 0, 0)
    for code in codes:
        if not code:
            continue
        f = nav_dir / f"{code}.json"
        if not f.exists():
            remote_store.ensure(f"nav_history/{code}.json")
        if not f.exists():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            hist = [h for h in (doc.get("history") or [])
                    if isinstance(h, dict)]
            if hist:
                # [BUG-C3] history files can be misordered on disk; the latest
                # point is the max-dated row, not necessarily the last one.
                latest = max(hist, key=lambda h: _dtkey(h.get("date")))
                d = latest.get("date")
                k = _dtkey(d)
                if k > best_key:  # '>' keeps the preferred plan on date ties
                    best = (latest.get("nav"), d)
                    best_key = k
        except Exception:
            continue
    if best is None and scheme and _num(scheme.get("nav")) is not None:
        return _num(scheme["nav"]), scheme.get("as_of") or ""
    return best


def _dtkey(d) -> tuple:
    """Sortable (year, month, day) for the many date formats used in the data
    (31-Oct-2025, 2026-07-31, July 31, 2026, 31-Jul-2026, ...)."""
    s = str(d or "").lower()
    m = re.search(r"(\d{1,2})\s*[- ]\s*([a-z]{3})[a-z]*[- ](\d{4})", s)
    if m:
        return (int(m.group(3)), _MONTHS.get(m.group(2)[:3], 0), int(m.group(1)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})[a-z]*,?\s+(\d{4})", s)
    if m:
        return (int(m.group(3)), _MONTHS.get(m.group(1)[:3], 0), int(m.group(2)))
    return (0, 0, 0)


def _scan_nav_history(idx: dict) -> None:
    global _LAST_ANOMALIES
    anomalies: list[dict] = []
    if not NAV_DIR.is_dir():
        _LAST_ANOMALIES = anomalies
        return
    for fn in os.listdir(NAV_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            doc = json.load(open(NAV_DIR / fn, encoding="utf-8"))
            hist = doc.get("history") or []
        except Exception:
            continue
        isin = (doc.get("isin") or "").strip().upper()
        # [BUG-F4] schema guard: history rows must be dicts with date+nav.
        bad = [h for h in hist if not isinstance(h, dict)]
        if bad and not [h for h in hist if isinstance(h, dict)]:
            anomalies.append({"file": fn, "problem": "history_not_dicts",
                              "rows": len(hist)})
            continue
        if bad:
            anomalies.append({"file": fn, "problem": "partial_non_dict_rows",
                              "rows": len(bad)})
        hist = [h for h in hist if isinstance(h, dict)]
        if not isin or not hist:
            continue
        # [BUG-C3] files may be stored misordered; pick the truly-latest row.
        last = max(hist, key=lambda h: _dtkey(h.get("date")))
        nav = _num(last.get("nav"))
        cur = idx.get(isin)
        if nav is not None and (cur is None or _dtkey(last.get("date")) > _dtkey(cur.get("date"))):
            idx[isin] = {"nav": nav, "date": last.get("date"),
                         "code": doc.get("scheme_code"), "fund": doc.get("fund_name")}
    _LAST_ANOMALIES = anomalies


def nav_schema_anomalies() -> list[dict]:
    """Malformed NAV-history files seen at the last index build (data-health)."""
    return list(_LAST_ANOMALIES)


def scan_nav_history_schema() -> list[dict]:
    """Standalone full pass over data/nav_history flagging files whose
    ``history`` rows are not {date, nav} dicts (the 97-file corruption class).
    Read-only; safe to call from data-health on its refresh cadence."""
    out: list[dict] = []
    if not NAV_DIR.is_dir():
        return out
    for fn in os.listdir(NAV_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            doc = json.load(open(NAV_DIR / fn, encoding="utf-8"))
            hist = doc.get("history") or []
        except Exception:
            continue
        if not hist:
            continue
        bad = sum(1 for h in hist if not isinstance(h, dict))
        if bad:
            out.append({"file": fn,
                        "problem": "all_rows_plain_strings" if bad == len(hist)
                        else f"{bad}/{len(hist)}_rows_not_dicts",
                        "rows": bad})
    return out


def _scan_stock_prices(idx: dict) -> None:
    if not STOCK_DIR.is_dir():
        return
    for fn in os.listdir(STOCK_DIR):
        if not fn.endswith(".json"):
            continue
        isin = fn[:-5].strip().upper()
        if not isin:
            continue
        try:
            doc = json.load(open(STOCK_DIR / fn, encoding="utf-8"))
        except Exception:
            continue
        hist = doc.get("history") or doc.get("prices") or doc.get("data") or []
        hist = [h for h in hist if isinstance(h, dict)]  # [BUG-F4] schema guard
        # [BUG-C3] defensively select the max-dated row, not the file-last row.
        last = max(hist, key=lambda h: _dtkey(h.get("date"))) if hist else None
        if not last:
            continue
        nav = _num(last.get("close") or last.get("price") or last.get("nav"))
        if nav is None:
            continue
        cur = idx.get(isin)
        if cur is None or _dtkey(last.get("date")) > _dtkey(cur.get("date")):
            idx[isin] = {"nav": nav, "date": last.get("date"), "price": True}


def _scan_universe_navs(idx: dict) -> None:
    """Add the universe (Combined NAV) NAVs stored on the schemes table."""
    try:
        from . import db as dbm
        con = dbm.WebDB().con
        rows = con.execute(
            "SELECT isin_regular, isin_direct, nav, as_of FROM schemes "
            "WHERE nav IS NOT NULL").fetchall()
        for r in rows:
            for isin in (r["isin_regular"], r["isin_direct"]):
                isin = (isin or "").strip().upper()
                if not isin:
                    continue
                cur = idx.get(isin)
                if cur is None or _dtkey(r["as_of"]) > _dtkey(cur.get("date")):
                    idx[isin] = {"nav": r["nav"], "date": r["as_of"] or ""}
    except Exception:
        pass


def _dirs_changed() -> bool:
    # Cheap staleness check: rebuild only when the cached index is missing or
    # old. Do NOT scan every NAV/price file on each call — that is slow.
    if not INDEX_PATH.exists():
        return True
    try:
        return time.time() - INDEX_PATH.stat().st_mtime > _MAX_INDEX_AGE
    except Exception:
        return True


def invalidate_index() -> None:
    try:
        INDEX_PATH.unlink()
    except OSError:
        pass


def latest_nav_index(force: bool = False) -> dict:
    """ISIN -> {nav, date, ...} using the most recent NAV/price available."""
    if force or _dirs_changed():
        idx: dict = {}
        _scan_nav_history(idx)
        _scan_universe_navs(idx)
        _scan_stock_prices(idx)
        try:
            INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            INDEX_PATH.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return idx
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return latest_nav_index(force=True)


def reweight_by_market_value(items: list[dict]) -> list[dict]:
    """Re-compute each item's ``weight`` from its CURRENT market value
    (units x latest NAV/price) instead of cost.

    [perf-v2.0.0 ALL-OR-NOTHING pricing] Market weighting applies only when
    EVERY unit-bearing item prices. The old mixed behaviour reweighted the
    priced lines among themselves while unpriced lines kept stale cost/manual
    weights — totals exceeded 100% and every pie chart silently renormalised
    the error away. Now: one unpriced line keeps the WHOLE portfolio on its
    existing weights, flagged ``pricing_basis: "cost"``.

    - items with ``units > 0`` and a price/NAV -> reweighted by market value;
    - any ``units > 0`` line without a price -> whole call falls back to cost;
    - items with ``units <= 0`` are dropped from the valued set;
    - items without ``units`` are manual weights, left untouched.
    Every returned item carries ``pricing_basis`` ("market" | "cost" |
    "manual") so the UI can state which basis backs the numbers.
    """
    if not items or not any(it.get("units") for it in items):
        return items
    idx = latest_nav_index()
    from . import db as dbm
    wdb = dbm.WebDB()

    def nav_for(it) -> tuple | None:
        """Freshest (nav, date) by date: nav-history (or price) vs the scheme's
        universe NAV — whichever has the later date wins."""
        isin = (it.get("isin") or "").strip().upper()
        rec = idx.get(isin) if isin else None
        cands: list[tuple] = []
        if rec and _num(rec.get("nav")) is not None and _num(rec["nav"]) > 0:
            cands.append((_dtkey(rec.get("date")), _num(rec["nav"]), rec.get("date") or ""))
        try:
            s = wdb._resolve_scheme_item({"isin": isin, "name": it.get("name") or ""})
            sn = _num(s.get("nav")) if s else None
            if sn is not None and sn > 0:
                cands.append((_dtkey(s.get("as_of")), sn, s.get("as_of") or ""))
            if s:
                prefer = "direct"
                if isin and isin == (s.get("isin_regular") or "").upper():
                    prefer = "regular"
                elif not (isin and isin == (s.get("isin_direct") or "").upper()) \
                        and "direct" not in (it.get("name") or "").lower():
                    prefer = "regular"
                got = scheme_latest_nav(s, prefer=prefer)
                if got and got[0] is not None and got[0] > 0:
                    cands.append((_dtkey(got[1]), got[0], got[1] or ""))
        except Exception:
            pass
        if not cands:
            return None
        cands.sort(key=lambda c: c[0])
        return cands[-1][1], cands[-1][2]

    valued: list[tuple[dict, float | None, float | None, str]] = []
    for it in items:
        units = _num(it.get("units"))
        if units is None:
            valued.append((it, None, None, "manual"))
            continue
        if units <= 0:
            continue  # zero-unit position is not a real holding
        got = nav_for(it)
        if got is None or got[0] is None or got[0] <= 0:
            valued.append((it, None, None, "cost"))  # unpriced unit line
        else:
            nav, nav_date = got
            it = dict(it)
            it["nav_date"] = _iso_date(nav_date) or ""
            valued.append((it, units * nav, nav, "market"))

    # [perf-v2.0.0] purity gate — see docstring.
    if any(basis == "cost" for _, mv, _, basis in valued):
        out = []
        for it, _mv, _nav, _basis in valued:
            it = dict(it)
            it["pricing_basis"] = "cost"
            out.append(it)
        return out

    total = sum(mv for _, mv, _, _ in valued if mv is not None and mv > 0)
    if total <= 0:
        return items
    out = []
    for it, mv, nav, basis in valued:
        it = dict(it)
        it["pricing_basis"] = basis
        if mv is None:
            out.append(it)
            continue
        it["weight"] = round(mv / total * 100, 4)
        it["current_value"] = round(mv, 2)
        it["nav"] = nav
        out.append(it)
    return out
