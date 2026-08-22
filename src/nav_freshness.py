"""NAV / price freshness check and backfill.

Scans the downloaded daily NAV history (``data/nav_history``) and stock price
history (``data/stock_history``) and reports any scheme/stock whose latest value
is older than ``max_age_days``. With ``backfill=True`` it re-pulls the history
for every stale scheme (from ``api.mfapi.in``, which returns the full daily
history up to today) so the gap between the last known value and the current
date is closed. Stock backfill delegates to ``src.stock_price``.

Run::

    python -m src.nav_freshness                  # check only
    python -m src.nav_freshness --backfill       # check + backfill NAVs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
NAV_DIR = BASE_DIR / "data" / "nav_history"
STOCK_DIR = BASE_DIR / "data" / "stock_history"

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def stale_days(date_str: str) -> int | None:
    """Days between a ``date_str`` (many formats) and today. None if unparseable."""
    d = date_str or ""
    s = str(d).lower()
    import re
    m = re.search(r"(\d{1,2})\s*[- ]\s*([a-z]{3})[a-z]*[- ](\d{4})", s)
    if m:
        y, mon, dd = int(m.group(3)), _MONTHS.get(m.group(2)[:3], 0), int(m.group(1))
    else:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            y, mon, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})[a-z]*,?\s+(\d{4})", s)
            if m:
                y, mon, dd = int(m.group(3)), _MONTHS.get(m.group(1)[:3], 0), int(m.group(2))
            else:
                return None
    if mon < 1 or mon > 12 or dd < 1:
        return None
    try:
        dt = date(y, mon, dd)
    except ValueError:
        return None
    return (date.today() - dt).days


def check_navs(max_age_days: int = 10) -> list[dict]:
    stale = []
    if not NAV_DIR.is_dir():
        return stale
    for fn in sorted(NAV_DIR.glob("*.json")):
        code = fn.stem
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            stale.append({"code": code, "error": "unreadable"})
            continue
        hist = doc.get("history") or []
        last = hist[-1].get("date") if hist else None
        days = stale_days(last) if last else None
        if not last or days is None or days > max_age_days:
            stale.append({"code": code, "fund": doc.get("fund_name") or "",
                          "last_date": last or None, "stale_days": days})
    return stale


def check_stocks(max_age_days: int = 10) -> list[dict]:
    stale = []
    if not STOCK_DIR.is_dir():
        return stale
    for fn in sorted(STOCK_DIR.glob("*.json")):
        isin = fn.stem
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            stale.append({"isin": isin, "error": "unreadable"})
            continue
        hist = doc.get("history") or doc.get("prices") or doc.get("data") or []
        last = hist[-1] if hist else None
        d = last.get("date") if last else None
        days = stale_days(d) if d else None
        if not d or days is None or days > max_age_days:
            stale.append({"isin": isin, "name": last.get("close") and doc.get("name") or "",
                          "last_date": d or None, "stale_days": days})
    return stale


def backfill_navs(codes: list[str]) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .fetch_missing_nav import _build_doc, _fetch

    def work(code: str) -> tuple[str, bool]:
        resp = _fetch(code)
        if resp is None:
            return code, False
        doc = _build_doc(code, resp)
        if not doc["history"]:
            return code, False
        (NAV_DIR / f"{code}.json").write_text(json.dumps(doc), encoding="utf-8")
        return code, True

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for code, success in pool.map(work, codes):
            if success:
                ok += 1
            else:
                failed += 1
    return {"attempted": len(codes), "ok": ok, "failed": failed}


def cas_sample_codes() -> list[str]:
    """AMFI scheme codes for the funds in the CAS sample portfolio (via ISINs)."""
    import json as _json
    cas_path = BASE_DIR / "CAS_sample_portfolio_holdings.json"
    if not cas_path.exists():
        return []
    try:
        doc = _json.loads(cas_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    isins = {(a.get("isin") or "").strip().upper()
             for a in (doc.get("portfolio_summary") or {}).get("allocations") or []}
    codes: list[str] = []

    def add(c):
        if c and c not in codes:
            codes.append(c)

    # 1) nav_history files keyed by their own isin field
    if NAV_DIR.is_dir():
        for fn in NAV_DIR.glob("*.json"):
            try:
                hd = json.loads(fn.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (hd.get("isin") or "").strip().upper() in isins:
                add(hd.get("scheme_code"))
    # 2) fall back to the market-value index (which may only hold the universe NAV)
    try:
        from webapp.market_value import latest_nav_index
        idx = latest_nav_index()
        for isin in isins:
            rec = idx.get(isin)
            if rec:
                add(rec.get("code"))
    except Exception:
        pass
    return codes


def backfill_codes_amfi(codes: list[str], days: int = 20) -> dict:
    """Fetch the recent AMFI NAV report and merge the points for the given
    codes into their nav_history files (the source the scheme-explorer charts
    read). Reports each code's last date after the refresh."""
    from datetime import timedelta
    from .nav_history import _date_key, _fetch_amfi, _parse_nav_text
    NAV_DIR.mkdir(parents=True, exist_ok=True)
    end = date.today()
    start = end - timedelta(days=days)
    text = _fetch_amfi(start, end)
    rows = _parse_nav_text(text)
    by_code: dict[str, list[tuple]] = {}
    for code, datestr, nav, name, plan, option, isin, isin_re in rows:
        if code in codes:
            by_code.setdefault(code, []).append(
                (datestr, nav, name, plan, option, isin, isin_re))
    updated = missing = 0
    after: list[dict] = []
    for code in codes:
        pts = by_code.get(code)
        if not pts:
            missing += 1
            after.append({"code": code, "fetched": 0, "last_date": None})
            continue
        path = NAV_DIR / f"{code}.json"
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                doc = {"scheme_code": code, "history": []}
        else:
            doc = {"scheme_code": code, "history": []}
        hist = doc.get("history") or []
        existing = {h.get("date") for h in hist}
        for datestr, nav, *_rest in pts:
            if datestr not in existing:
                hist.append({"date": datestr, "nav": nav})
                existing.add(datestr)
        hist.sort(key=lambda h: _date_key(h.get("date", "")))
        doc["history"] = hist
        doc["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(doc), encoding="utf-8")
        updated += 1
        after.append({"code": code, "fetched": len(pts),
                      "last_date": hist[-1]["date"] if hist else None})
    return {"window": f"{start.isoformat()}..{end.isoformat()}",
            "codes_requested": len(codes), "updated": updated,
            "no_data_in_window": missing, "after": after}


def portfolio_stale(items: list[dict], max_age_days: int = 10) -> list[dict]:
    """Held items whose latest NAV/price is missing or older than max_age_days."""
    out = []
    for it in items or []:
        d = it.get("nav_date")
        days = stale_days(d) if d else None
        if not d or days is None or days > max_age_days:
            out.append({"name": it.get("name"), "isin": (it.get("isin") or ""),
                        "nav": it.get("nav"), "nav_date": d or None,
                        "stale_days": days})
    return out


def all_codes() -> list[str]:
    if not NAV_DIR.is_dir():
        return []
    return sorted(fn.stem for fn in NAV_DIR.glob("*.json"))


def completeness_report(latest_expected: str | None = None) -> dict:
    """Status of every scheme's nav_history: earliest/latest date, point count,
    largest gap and whether it is COMPLETE from inception to the latest NAV."""
    if latest_expected is None:
        latest_expected = date.today().isoformat()
    import datetime as _dt
    from webapp.market_value import _dtkey

    def as_date(s):
        k = _dtkey(s)
        if k == (0, 0, 0):
            return None
        try:
            return _dt.date(k[0], k[1], k[2])
        except ValueError:
            return None

    complete: list[dict] = []
    incomplete: list[dict] = []
    for fn in sorted(NAV_DIR.glob("*.json")):
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            incomplete.append({"code": fn.stem, "error": "unreadable"})
            continue
        code = doc.get("scheme_code") or fn.stem
        hist = doc.get("history") or []
        fund = doc.get("fund_name") or ""
        if not hist:
            incomplete.append({"code": code, "fund": fund, "reason": "empty history"})
            continue
        dates = [as_date(h.get("date")) for h in hist]
        valid = [d for d in dates if d]
        earliest = hist[0]["date"]
        latest = hist[-1]["date"]
        max_gap = 0
        for i in range(1, len(valid)):
            gap = (valid[i] - valid[i - 1]).days
            if gap > max_gap:
                max_gap = gap
        last_days = stale_days(latest)
        recent = last_days is not None and last_days <= 5
        gap_ok = max_gap <= 90
        rec = {"code": code, "fund": fund, "earliest": earliest, "latest": latest,
               "points": len(hist), "max_gap_days": max_gap, "last_age_days": last_days,
               "complete": bool(valid and recent and gap_ok)}
        (complete if rec["complete"] else incomplete).append(rec)

    return {"total": len(complete) + len(incomplete),
            "complete": len(complete), "incomplete": len(incomplete),
            "latest_expected": latest_expected,
            "complete_schemes": complete, "incomplete_schemes": incomplete}


def run_freshness(max_age_days: int = 10, backfill: bool = False) -> dict:
    navs = check_navs(max_age_days)
    stocks = check_stocks(max_age_days)
    report = {"as_of": date.today().isoformat(), "max_age_days": max_age_days,
              "stale_navs": navs, "stale_stocks": stocks,
              "backfilled": None}
    if backfill and navs:
        codes = [n["code"] for n in navs if n.get("code")]
        report["backfilled"] = backfill_navs(codes)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age", type=int, default=10)
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()
    report = run_freshness(max_age_days=args.max_age, backfill=args.backfill)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())