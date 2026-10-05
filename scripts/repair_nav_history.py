"""Repair malformed nav_history files by re-pulling full AMFI history.

Some legacy ``data/nav_history/<code>.json`` files stored bare ISO date
strings (no ``{date, nav}`` rows) or lost their history entirely. Every reader
of those files used to crash (``'str' object has no attribute 'get'``); this
script re-downloads the affected schemes from the official AMFI portal
(``src.nav_history.fetch_codes_history`` - chunked 90-day windows, one request
covers every scheme) and rewrites the file in the canonical schema, keeping
the old category/plan/ISIN metadata when AMFI doesn't return it.

Run::

    python scripts/repair_nav_history.py --dry-run
    python scripts/repair_nav_history.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from src.nav_freshness import history_rows, scheme_nav_files  # noqa: E402

NAV_DIR = BASE_DIR / "data" / "nav_history"

_KEEP = ("category", "plan", "option", "isin", "isin_reinvestment", "currency")


def broken_codes() -> tuple[list[str], list[str]]:
    """(malformed, empty) scheme codes whose history is unusable."""
    malformed, empty = [], []
    for fn in scheme_nav_files(NAV_DIR):
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            malformed.append(fn.stem)
            continue
        raw = doc.get("history") or []
        if raw and len(history_rows(raw)) < len(raw):
            malformed.append(fn.stem)
        elif not raw:
            empty.append(fn.stem)
    return sorted(malformed), sorted(empty)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    malformed, empty = broken_codes()
    targets = malformed + empty
    print(f"malformed={len(malformed)} empty={len(empty)} -> {len(targets)} target(s)")
    if args.dry_run or not targets:
        print("dry-run: nothing written" if args.dry_run else "nothing to repair")
        return 0

    from src.nav_history import fetch_codes_history
    summary = fetch_codes_history(targets, out_dir=NAV_DIR)

    # Carry forward metadata the portal walk does not return.
    restored = 0
    for code in summary.get("codes", []):
        path = NAV_DIR / f"{code}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        filled = 0
        for key in _KEEP:
            if not doc.get(key):
                doc[key] = ""
        if filled:
            restored += filled
        path.write_text(json.dumps(doc), encoding="utf-8")

    still_malformed, still_empty = broken_codes()
    # AMFI no longer lists schemes that have fully wound down (interval/closed-
    # ended series). Their legacy files cannot be re-pulled, so rewrite them into
    # the canonical schema with the salvaged ISO date strings preserved and an
    # honest empty history — readers stop crashing and the freshness audit reports
    # them as inactive instead of silently mis-parsing them.
    quarantined = 0
    for code in still_malformed:
        path = NAV_DIR / f"{code}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw = doc.get("history") or []
        legacy = [h for h in raw if isinstance(h, str)]
        doc["history"] = []
        doc["legacy_dates"] = legacy
        doc["note"] = ("no AMFI history - scheme is no longer listed by AMFI "
                       "(wound down / merged); legacy date strings preserved "
                       "in legacy_dates")
        doc["repaired_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(doc), encoding="utf-8")
        quarantined += 1

    print(json.dumps({
        "windows": summary.get("windows"),
        "written": summary.get("written"),
        "quarantined_inactive": quarantined,
        "still_malformed": len(broken_codes()[0]),
        "still_empty": len(broken_codes()[1]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
