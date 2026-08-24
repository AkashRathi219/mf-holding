"""Per-scheme NAV freshness & correctness audit engine [NAV-FRESH].

Measures EVERY nav_history file against the AMFI publication calendar
(see src.nav_freshness) instead of raw day-gaps, then three-way
spot-checks a random sample for CORRECTNESS, not just freshness:

    history file  ->  live AMFI NAVAll row  ->  schemes DB row

Lives under src/ so the webapp boot graph stays inside the slim-deps
allowlist (tests/test_slim_deps.py); the CLI wrapper is
scripts/audit_nav_freshness.py.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .nav_freshness import classify, expected_latest_nav_date  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent
NAV_DIR = BASE_DIR / "data" / "nav_history"
STOCK_DIR = BASE_DIR / "data" / "stock_history"
REPORT_DIR = BASE_DIR / "data" / "reports"

BUCKET_ORDER = ("current", "lag1", "stale_recent", "stale_deep",
                "dead_suspect", "no_history")


def _last_point(path: Path) -> tuple | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    hist = doc.get("history") or []
    if not hist:
        return (doc.get("scheme_code") or path.stem, doc.get("fund_name") or "",
                None, None)
    last = hist[-1]
    return (doc.get("scheme_code") or path.stem, doc.get("fund_name") or "",
            last.get("date"), last.get("nav"))


def audit_corpus(expected: date) -> tuple[Counter, list[dict]]:
    """Classify every local nav_history file. Returns (buckets, rows)."""
    from .nav_freshness import classify
    buckets: Counter = Counter()
    rows: list[dict] = []
    if not NAV_DIR.is_dir():
        return buckets, rows
    for fn in sorted(NAV_DIR.glob("*.json")):
        code, fund, last_date, last_nav = _last_point(fn) or (fn.stem, "", None, None)
        cls = classify(last_date, expected=expected)
        buckets[cls["bucket"]] += 1
        rows.append({"code": code, "fund": fund, "last_date": last_date or "",
                     "last_nav": last_nav, "bucket": cls["bucket"],
                     "publish_gap": cls["publish_gap"],
                     "gap_days": cls["gap_days"],
                     "expected": cls["expected"] or ""})
    for b in BUCKET_ORDER:
        buckets.setdefault(b, 0)
    return buckets, rows


def r2_coverage() -> dict | None:
    """nav_history objects in R2 vs local (cold-start coverage), best-effort."""
    try:
        from webapp import remote_store
        if not remote_store.is_configured():
            return None
        if not hasattr(remote_store, "list_objects"):
            return None
        keys = [k for k in remote_store.list_objects("nav_history/")
                if k.endswith(".json")]
        if not keys:
            return None
        local = {fn.name for fn in NAV_DIR.glob("*.json")} if NAV_DIR.is_dir() else set()
        remote_names = {k.split("/")[-1] for k in keys}
        return {"r2_objects": len(remote_names), "local_files": len(local),
                "remote_only": len(remote_names - local),
                "local_only": len(local - remote_names)}
    except Exception as e:
        return {"error": str(e)[:160]}


def _live_nav_rows(expected: date, days: int = 10) -> dict[str, tuple]:
    """code -> (datestr, nav) from one bulk AMFI fetch ending at `expected`."""
    from .nav_history import _fetch_amfi, _parse_nav_text
    end = expected + timedelta(days=1)
    start = end - timedelta(days=days)
    text = _fetch_amfi(start, end)
    out: dict[str, tuple] = {}
    for code, datestr, nav, *_ in _parse_nav_text(text):
        prev = out.get(code)
        if prev is None or datestr > prev[0]:
            out[code] = (datestr, nav)
    return out


def three_way_sample(expected: date, sample: int, live: bool) -> dict:
    """Random-sample correctness check: history vs live AMFI vs schemes DB."""
    files = sorted(NAV_DIR.glob("*.json")) if NAV_DIR.is_dir() else []
    with_points = []
    for fn in files:
        pt = _last_point(fn)
        if pt and pt[2]:
            with_points.append((fn.stem, pt))
    if not with_points or sample <= 0:
        return {"sample_size": 0}
    picked = random.sample(with_points, min(sample, len(with_points)))

    live_rows: dict[str, tuple] = {}
    live_error = None
    if live:
        try:
            live_rows = _live_nav_rows(expected)
        except Exception as e:
            live_error = str(e)[:160]

    db_rows: dict[str, tuple] = {}
    db_error = None
    try:
        from webapp.db import WebDB
        con = WebDB().con
        for code, _pt in picked:
            row = con.execute(
                "SELECT fund_name, nav FROM schemes WHERE amfi_regular=? "
                "OR amfi_direct=? LIMIT 1", (code, code)).fetchone()
            if row:
                db_rows[code] = (row["fund_name"], row["nav"])
    except Exception as e:
        db_error = str(e)[:160]

    items = []
    for code, (_c, fund, last_date, last_nav) in picked:
        live = live_rows.get(code)
        db = db_rows.get(code)
        items.append({
            "code": code, "fund": fund,
            "history": {"date": last_date, "nav": last_nav},
            "live": {"date": live[0], "nav": live[1]} if live else None,
            "db_nav": db[1] if db else None,
            "date_current": bool(live and last_date and live[0] == last_date),
            "nav_match": bool(live and last_nav is not None and live[1] is not None
                              and abs(float(live[1]) - float(last_nav)) < 1e-4),
            "db_nav_match": bool(db and db[1] is not None and last_nav is not None
                                 and abs(float(db[1]) - float(last_nav)) < 0.01),
        })
    ok = [i for i in items if i["live"]]
    return {
        "sample_size": len(items),
        "live_checked": len(ok),
        "live_error": live_error,
        "db_error": db_error,
        "date_current": sum(1 for i in ok if i["date_current"]),
        "nav_match": sum(1 for i in ok if i["nav_match"]),
        "db_nav_match": sum(1 for i in items if i["db_nav_match"]),
        "items": items,
    }


def stock_audit(expected: date, max_stale_days: int = 7) -> dict:
    """Stock price files vs the same publication calendar (equity closes
    publish by ~18:00 IST on trading days; the NAV cutoff is conservative
    here, so 'lag1' is normal for stocks)."""
    from .nav_freshness import check_stocks
    stale = check_stocks(max_age_days=max_stale_days)
    total = len(list(STOCK_DIR.glob("*.json"))) if STOCK_DIR.is_dir() else 0
    return {"files": total, "stale_gt_7d": len(stale),
            "expected": expected.isoformat(),
            "examples": stale[:10]}


def run_audit(sample: int = 5, live: bool = True, csv_out: str | None = "auto",
              with_stocks: bool = False) -> dict:
    expected = expected_latest_nav_date()
    buckets, rows = audit_corpus(expected)
    report = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "expected_latest": expected.isoformat(),
        "files_scanned": sum(buckets.values()),
        "buckets": {b: buckets.get(b, 0) for b in BUCKET_ORDER},
        "healthy_pct": round(
            (buckets.get("current", 0) + buckets.get("lag1", 0))
            / max(1, sum(buckets.values())) * 100, 2),
        "r2": r2_coverage(),
        "sample": three_way_sample(expected, sample, live),
    }
    if with_stocks:
        report["stocks"] = stock_audit(expected)

    offenders = sorted(
        (r for r in rows if r["bucket"] not in ("current", "lag1")),
        key=lambda r: (-(r["publish_gap"] or 0), r["code"]))
    report["offenders"] = offenders[:20]
    report["offender_count"] = len(offenders)

    if csv_out == "auto":
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        csv_out = str(REPORT_DIR / f"nav_freshness_{date.today().isoformat()}.csv")
    if csv_out:
        import csv as _csv
        with open(csv_out, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=[
                "code", "fund", "last_date", "last_nav", "bucket",
                "publish_gap", "gap_days", "expected"])
            w.writeheader()
            w.writerows(rows)
        report["csv"] = csv_out
    return report
