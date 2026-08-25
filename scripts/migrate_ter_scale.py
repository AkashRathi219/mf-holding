"""One-time repair of percent-scale contamination in rate-metric columns.

[perf-v2.0.0 audit finding #3] The universe/schemes tables store TER / YTM /
duration / avg-maturity as FRACTIONS (0.0072 = 0.72%), but a handful of rows
arrived on the PERCENT scale (WhiteOak Aggressive Hybrid ter_direct = 0.2778,
rendered by the UI's x100 as "29.61%"). Ingest now runs every value through
webapp.conventions.normalize_metric(); this script repairs EXISTING databases.

Usage:
    python scripts/migrate_ter_scale.py            # dry run (default)
    python scripts/migrate_ter_scale.py --apply    # write fixes

Idempotent: already-clean rows are left untouched.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.conventions import METRIC_BANDS  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "webapp.db"

# column -> plausibility kind (stored unit = fraction)
COLS = {"ter": "ter", "ter_regular": "ter", "ter_direct": "ter",
        "ytm": "ytm", "duration": "duration", "avg_maturity": "avg_maturity"}


def fix_row(col: str, v):
    if v is None:
        return None, False
    lo, hi = METRIC_BANDS.get(COLS[col], (float("-inf"), float("inf")))
    if lo <= v <= hi:
        return v, False
    scaled = round(v / 100.0, 10)
    if v > hi and lo <= scaled <= hi:
        return scaled, True  # percent-scale contamination -> rescale
    return v, False          # implausible either way: leave for review


def main() -> int:
    apply = "--apply" in sys.argv
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 1
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cols = [c[1] for c in con.execute("PRAGMA table_info(schemes)").fetchall()]
    usable = [c for c in COLS if c in cols]
    sel = ", ".join(["id", "fund_name"] + usable)
    changed = 0
    for row in con.execute(f"SELECT {sel} FROM schemes").fetchall():
        updates = {}
        for col in usable:
            fixed, did = fix_row(col, row[col])
            if did:
                updates[col] = fixed
        if updates:
            changed += 1
            print(f"[{'FIX' if apply else 'DRY'}] id={row['id']} "
                  f"{row['fund_name'][:52]}")
            for col, new in updates.items():
                print(f"      {col}: {row[col]} -> {new}")
            if apply:
                sets = ", ".join(f"{c}=?" for c in updates)
                con.execute(f"UPDATE schemes SET {sets} WHERE id=?",
                            (*updates.values(), row["id"]))
    if apply:
        con.commit()
    con.close()
    print(f"\n{changed} row(s) {'repaired' if apply else 'NEED repair'} "
          f"({'applied' if apply else 'dry run — rerun with --apply'}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
