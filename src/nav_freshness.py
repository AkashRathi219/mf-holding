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
import logging
import sys
from datetime import date, datetime, time as _time, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
NAV_DIR = BASE_DIR / "data" / "nav_history"
STOCK_DIR = BASE_DIR / "data" / "stock_history"

logger = logging.getLogger(__name__)


def scheme_nav_files(nav_dir: Path | None = None) -> list[Path]:
    """[BUG] Real scheme-code files only: sentinel manifests (manifest.json,
    download_summary.json) share data/nav_history/ and were previously counted
    as schemes by freshness/audit/backfill --all."""
    d = nav_dir or NAV_DIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.stem.isdigit())

# ---------------------------------------------------------------------------
# AMFI publication calendar [NAV-FRESH]
# ---------------------------------------------------------------------------
# AMFI publishes day-T NAVs late in the evening of T (~23:00 IST, sometimes
# later); nothing publishes on weekends. On a Monday morning the freshest
# possible NAV is therefore Friday's — "latest NAV 21-Aug" seen on 24-Aug-2026
# is CORRECT, not a pipeline failure. The audit measures every scheme against
# this calendar (publish-day gaps) instead of raw calendar-day gaps.
#
# NSE equity holidays 2026. Advisory: verify against the NSE annual circular
# each January; a wrong/missing entry merely shifts a scheme between
# `current` and `lag1` (the grace bucket) and never hides a stale scheme.
# Override/extend per year without code edits via
# data/reference/nse_holidays_<year>.json ({"<YYYY-MM-DD>": "name", ...}).

NSE_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 4),    # Holi
    date(2026, 3, 20),   # Id-Ul-Fitr (approx - verify circular)
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # Bakri Id (approx - verify circular)
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 9),   # Diwali-Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti (approx - verify circular)
    date(2026, 12, 25),  # Christmas
})

_HOLIDAY_CACHE: dict[int, frozenset] = {}
_HOLIDAY_WARNED: set[int] = set()


def nse_holidays(year: int) -> frozenset:
    """Built-in holiday set for `year`, overridable per-year via
    data/reference/nse_holidays_<year>.json."""
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    days = NSE_HOLIDAYS_2026 if year == 2026 else frozenset()
    path = BASE_DIR / "data" / "reference" / f"nse_holidays_{year}.json"
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            days = frozenset(
                date.fromisoformat(k) for k in (doc.get("holidays") or doc.keys())
                if isinstance(k, str) and len(k) == 10)
        except Exception:
            pass
    elif not days and year not in _HOLIDAY_WARNED:
        # [BUG-L16] a silently-empty calendar misclassifies publish-days from
        # this year on; say so once, loudly.
        logger.warning(
            "No NSE holiday calendar for %d (builtin covers 2026 only). Add "
            "data/reference/nse_holidays_%d.json or freshness buckets will "
            "drift.", year, year)
        _HOLIDAY_WARNED.add(year)
    _HOLIDAY_CACHE[year] = days
    return days


def is_publish_day(d: date, year_days: frozenset | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    hol = year_days if year_days is not None else nse_holidays(d.year)
    return d not in hol


def prev_publish_day(d: date) -> date:
    d = d - timedelta(days=1)
    while not is_publish_day(d):
        d -= timedelta(days=1)
    return d


# AMFI's final NAVAll for day T lands ~23:00 IST; before that, "latest" is T-1.
AMFI_PUBLISH_CUTOFF = _time(23, 0)


def expected_latest_nav_date(now: datetime | None = None) -> date:
    """The newest NAV date that AMFI could have published as of `now` (IST)."""
    if now is None:
        from .refresh_log import IST
        now = datetime.now(IST)
    today = now.date()
    cutoff = now.time() >= AMFI_PUBLISH_CUTOFF
    if cutoff and is_publish_day(today):
        return today
    return prev_publish_day(today)


def publish_days_between(last: date, expected: date) -> int:
    """Publish days missed: count of publish days in (last, expected]."""
    if expected <= last:
        return 0
    n, d = 0, last
    while d < expected:
        d += timedelta(days=1)
        if is_publish_day(d):
            n += 1
    return n


# Buckets by publish-days missed. lag1 = one publish day behind: late-publish
# AMFs / report lag make this normal; the audit reports it, the UI forgives it.
BUCKETS = ("current", "lag1", "stale_recent", "stale_deep", "dead_suspect",
           "no_history")


def parse_date_any(s: str) -> date | None:
    """'21-Aug-2026' / '2026-08-21' / 'Aug 21, 2026' -> date; None if unparseable."""
    import re
    d = s or ""
    txt = str(d).lower()
    m = re.search(r"(\d{1,2})\s*[- ]\s*([a-z]{3})[a-z]*[- ](\d{4})", txt)
    if m:
        y, mon, dd = int(m.group(3)), _MONTHS.get(m.group(2)[:3], 0), int(m.group(1))
    else:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", txt)
        if m:
            y, mon, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"([a-z]{3})[a-z]*\s+(\d{1,2})[a-z]*,?\s+(\d{4})", txt)
            if m:
                y, mon, dd = int(m.group(3)), _MONTHS.get(m.group(1)[:3], 0), int(m.group(2))
            else:
                return None
    if mon < 1 or mon > 12 or dd < 1:
        return None
    try:
        return date(y, mon, dd)
    except ValueError:
        return None


def classify(last_date_str: str | None, now: datetime | None = None,
             expected: date | None = None) -> dict:
    """Bucket one scheme's latest NAV date against the publication calendar."""
    if not last_date_str:
        return {"bucket": "no_history", "publish_gap": None, "gap_days": None,
                "expected": expected.isoformat() if expected else None}
    exp = expected or expected_latest_nav_date(now)
    ld = parse_date_any(str(last_date_str))
    if ld is None:
        return {"bucket": "no_history", "publish_gap": None, "gap_days": None,
                "expected": exp.isoformat()}
    gap = publish_days_between(ld, exp)
    if gap == 0:
        bucket = "current"
    elif gap == 1:
        bucket = "lag1"
    elif gap <= 6:
        bucket = "stale_recent"
    elif gap < 45:
        bucket = "stale_deep"
    else:
        bucket = "dead_suspect"
    return {"bucket": bucket, "publish_gap": gap,
            "gap_days": (exp - ld).days, "expected": exp.isoformat()}


# ---------------------------------------------------------------------------

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
    for fn in scheme_nav_files():
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
    for fn in scheme_nav_files():
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
    return sorted(fn.stem for fn in scheme_nav_files())


def scheme_history_completeness(code: str) -> dict | None:
    """Completeness stats for ONE scheme's nav_history file; None when the
    file is missing/unreadable. Same rules as completeness_report, scoped to
    a single code so the scheme-details API can badge its history."""
    path = NAV_DIR / f"{code}.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
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

    hist = doc.get("history") or []
    if not hist:
        return {"code": code, "points": 0, "complete": False}
    dates = [as_date(h.get("date")) for h in hist]
    valid = [d for d in dates if d]
    max_gap = 0
    for i in range(1, len(valid)):
        max_gap = max(max_gap, (valid[i] - valid[i - 1]).days)
    last_days = stale_days(hist[-1].get("date"))
    recent = last_days is not None and last_days <= 5
    return {"code": code, "points": len(hist),
            "earliest": hist[0].get("date"), "latest": hist[-1].get("date"),
            "max_gap_days": max_gap, "last_age_days": last_days,
            "complete": bool(valid and recent and max_gap <= 90)}


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
    for fn in scheme_nav_files():
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