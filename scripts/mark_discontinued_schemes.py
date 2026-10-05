"""Mark schemes AMFI no longer publishes as discontinued and record their last
known NAV.

A scheme is flagged ``discontinued`` when its newest point in
``data/nav_history/<code>.json`` is older than ``--min-age-days`` (default 30)
AND the code is absent from AMFI's live NAV report — i.e. the scheme has wound
down, been merged or been renamed away, and no fresh NAV can ever be pulled.
(Schemes that are merely late but still published stay out of this list; they
are handled by ``nav-freshness --backfill``.)

Outputs
-------
* ``data/reference/discontinued_schemes.json`` — code, fund, AMC, last known
  NAV date + NAV value, point count, reason (source of truth).
* ``data/reference/discontinued_schemes.csv``  — same, for the coverage tag.
* ``data/webapp.db`` — ``schemes.coverage='discontinued'`` for the matched
  schemes so /api/schemes reports the status.
* ``<frontend>/public/data/discontinued.json`` — for the dashboard panel.

Run::

    python scripts/mark_discontinued_schemes.py            # write + patch DB
    python scripts/mark_discontinued_schemes.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from src.nav_freshness import history_rows, scheme_nav_files  # noqa: E402
from webapp.market_value import _dtkey  # noqa: E402

NAV_DIR = BASE_DIR / "data" / "nav_history"
REF_DIR = BASE_DIR / "data" / "reference"
DB_PATH = BASE_DIR / "data" / "webapp.db"
FRONTEND_JSON = (BASE_DIR.parent / "mfholding_frontend" / "welcome-gateway"
                 / "public" / "data" / "discontinued.json")
JSON_OUT = REF_DIR / "discontinued_schemes.json"
CSV_OUT = REF_DIR / "discontinued_schemes.csv"


def live_amfi_codes(lookback_days: int = 10) -> set[str]:
    """Codes AMFI published a NAV for within the last ``lookback_days`` days."""
    from src.nav_history import _fetch_amfi, _parse_nav_text
    end = date.today()
    text = _fetch_amfi(end - timedelta(days=lookback_days), end)
    return {r[0] for r in _parse_nav_text(text)}


def _last_point(doc: dict) -> tuple[str | None, float | None, int]:
    rows = history_rows(doc.get("history"))
    if rows:
        last = max(rows, key=lambda h: _dtkey(h.get("date")))
        return last.get("date"), last.get("nav"), len(rows)
    legacy = doc.get("legacy_dates") or []
    if legacy:
        return max(legacy), None, len(legacy)
    return None, None, 0


def _db_meta() -> dict[str, tuple[str, str]]:
    """scheme_code -> (fund_name, amc) from the webapp DB (best effort)."""
    if not DB_PATH.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    try:
        con = sqlite3.connect(DB_PATH)
        for reg, direct, fund, amc in con.execute(
                "SELECT amfi_regular, amfi_direct, fund_name, amc FROM schemes"):
            for code in (reg, direct):
                if code:
                    out.setdefault(str(code), (fund or "", amc or ""))
        con.close()
    except sqlite3.Error:
        pass
    return out


def collect(min_age_days: int) -> dict:
    live = live_amfi_codes()
    meta = _db_meta()
    today = date.today()
    schemes: list[dict] = []

    for fn in scheme_nav_files(NAV_DIR):
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        code = fn.stem
        last_date, last_nav, points = _last_point(doc)
        if not last_date:
            continue
        key = _dtkey(last_date)
        if key == (0, 0, 0):
            continue
        age = (today - date(*key)).days
        if age <= min_age_days or code in live:
            continue
        fund, amc = meta.get(code, (doc.get("fund_name") or "", ""))
        schemes.append({
            "scheme_code": code,
            "fund_name": fund or doc.get("fund_name") or "",
            "amc": amc,
            "last_nav_date": last_date,
            "last_nav": last_nav,
            "history_points": points,
            "stale_days": age,
            "reason": ("scheme no longer listed in AMFI NAV report "
                       "(discontinued / wound down / merged)"),
            "status": "discontinued",
        })

    schemes.sort(key=lambda s: (-s["stale_days"], s["fund_name"]))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "min_age_days": min_age_days,
        "count": len(schemes),
        "schemes": schemes,
    }


def write_outputs(payload: dict) -> None:
    REF_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scheme_code", "fund", "amc", "last_nav_date", "last_nav",
                    "history_points", "stale_days", "reason"])
        for s in payload["schemes"]:
            w.writerow([s["scheme_code"], s["fund_name"], s["amc"], s["last_nav_date"],
                        s["last_nav"], s["history_points"], s["stale_days"], s["reason"]])
    FRONTEND_JSON.parent.mkdir(parents=True, exist_ok=True)
    FRONTEND_JSON.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def patch_db(payload: dict) -> int:
    """Flag the matched schemes in webapp.db (coverage='discontinued')."""
    codes = {s["scheme_code"] for s in payload["schemes"]}
    if not codes or not DB_PATH.exists():
        return 0
    con = sqlite3.connect(DB_PATH)
    updated = 0
    for code in codes:
        cur = con.execute(
            "UPDATE schemes SET coverage='discontinued' "
            "WHERE amfi_regular=? OR amfi_direct=?", (code, code))
        updated += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    con.commit()
    con.close()
    return updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = collect(args.min_age_days)
    print(f"discontinued schemes: {payload['count']} "
          f"(min age {args.min_age_days}d)")
    for s in payload["schemes"][:10]:
        print(f"  {s['scheme_code']}  {s['fund_name'][:52]:<52} "
              f"last NAV {s['last_nav']} @ {s['last_nav_date']}")
    if args.dry_run:
        return 0

    write_outputs(payload)
    updated = patch_db(payload)
    print(f"wrote {JSON_OUT.name}, {CSV_OUT.name}, {FRONTEND_JSON.name}; "
          f"db rows flagged: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
