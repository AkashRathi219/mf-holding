"""Audit nav_history files for thin stubs shadowing full R2 histories.

Cold-start stubs [NAV-STUB]: an old nav_daily run on an empty data directory
created recent-window-only files (< 30 points). webapp.remote_store treats an
existing local file as authoritative and never re-fetches, so such a stub
permanently shadows the full since-inception history on R2 and analytics
honestly reports "not enough NAV history" for it.

This tool:
  1. sweeps every scheme in webapp.db (both plan codes) + every history file;
  2. classifies each as ok / stub / no_file;
  3. with --heal, replaces a stub with a better copy: the R2 object when it
     has more points, else the mfapi full-history mirror (network).

Usage:
    python scripts/audit_nav_history.py               # report only
    python scripts/audit_nav_history.py --heal        # upgrade stubs in place
    python scripts/audit_nav_history.py --heal --limit 50
    python scripts/audit_nav_history.py --json        # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp import remote_store  # noqa: E402

DATA_DIR = ROOT / "data"
NAV_DIR = DATA_DIR / "nav_history"
DB_PATH = DATA_DIR / "webapp.db"
STUB_MIN_POINTS = 30  # keep in sync with webapp.db.NAV_STUB_HEAL_MIN_POINTS

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _parse_nav_date(s: str) -> date | None:
    try:
        d, m, y = str(s or "").strip().split("-")
        return date(int(y), _MONTHS[m.lower()[:3]], int(d))
    except Exception:
        return None


def _load_doc(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _classify(doc: dict | None) -> tuple[str, int, str | None, str | None]:
    """(status, points, first_date, last_date) for one history file."""
    if doc is None:
        return "no_file", 0, None, None
    hist = doc.get("history") or []
    if not hist:
        return "stub", 0, None, None
    dates = [_parse_nav_date(h.get("date")) for h in hist]
    valid = [d for d in dates if d]
    first = min(valid).isoformat() if valid else None
    last = max(valid).isoformat() if valid else None
    status = "ok" if len(hist) >= STUB_MIN_POINTS else "stub"
    return status, len(hist), first, last


def _heal_code(code: str, local_points: int) -> tuple[bool, int, str]:
    """Upgrade one stub in place. Returns (healed, new_points, source)."""
    path = NAV_DIR / f"{code}.json"
    tmp = path.parent / (path.name + ".heal")
    try:
        if remote_store.download_to(f"nav_history/{code}.json", tmp) is not None:
            cand = json.loads(tmp.read_text(encoding="utf-8"))
            n = len(cand.get("history") or [])
            if n > local_points:
                tmp.replace(path)
                return True, n, "r2"
            tmp.unlink()
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    try:
        from src.fetch_missing_nav import _build_doc, _fetch
        resp = _fetch(code)
        if resp is not None:
            cand = _build_doc(code, resp)
            n = len(cand.get("history") or [])
            if n > local_points:
                path.write_text(json.dumps(cand), encoding="utf-8")
                return True, n, "mfapi"
    except Exception:
        pass
    return False, local_points, "none"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--heal", action="store_true",
                    help="replace stubs with better R2/mfapi copies")
    ap.add_argument("--limit", type=int, default=0,
                    help="max codes to heal this run (0 = no cap)")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit JSON instead of a table")
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    schemes = con.execute(
        "SELECT id, fund_name, amfi_regular, amfi_direct FROM schemes"
    ).fetchall()
    con.close()

    # A code can serve several schemes (regular == direct for ETFs); audit codes.
    code_to_schemes: dict[str, list[int]] = {}
    for r in schemes:
        for c in (r["amfi_direct"], r["amfi_regular"]):
            if c:
                code_to_schemes.setdefault(c, []).append(r["id"])

    rows = []
    for code in sorted(code_to_schemes):
        doc = _load_doc(NAV_DIR / f"{code}.json")
        status, pts, first, last = _classify(doc)
        rows.append({"code": code, "status": status, "points": pts,
                     "first": first, "last": last,
                     "schemes": code_to_schemes[code]})

    counts = {"ok": 0, "stub": 0, "no_file": 0}
    for r in rows:
        counts[r["status"]] += 1

    if args.as_json and not args.heal:
        print(json.dumps({"total_codes": len(rows), "counts": counts,
                          "stubs": [r for r in rows if r["status"] == "stub"]},
                         indent=1))
        return 0

    print(f"schemes: {len(schemes)}  distinct codes: {len(rows)}  "
          f"ok: {counts['ok']}  stub(<{STUB_MIN_POINTS} pts): {counts['stub']}  "
          f"no_file: {counts['no_file']}")
    stubs = [r for r in rows if r["status"] == "stub"]
    for r in stubs[:25]:
        print(f"  STUB {r['code']:>8}  pts={r['points']:<4} "
              f"{r['first'] or '-'} -> {r['last'] or '-'}  "
              f"scheme_ids={r['schemes'][:3]}")
    if len(stubs) > 25:
        print(f"  ... and {len(stubs) - 25} more")

    if not args.heal:
        print("run with --heal to upgrade stubs from R2/mfapi")
        return 0

    healed = failed = 0
    for r in stubs:
        if args.limit and healed >= args.limit:
            break
        ok, n, src = _heal_code(r["code"], r["points"])
        if ok:
            healed += 1
            print(f"  HEALED {r['code']:>8}  {r['points']} -> {n} pts ({src})")
        else:
            failed += 1
    print(f"healed {healed}, failed {failed} "
          f"(failed = no better copy on R2/mfapi; often genuinely young funds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
