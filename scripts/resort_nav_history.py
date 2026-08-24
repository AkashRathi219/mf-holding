#!/usr/bin/env python3
"""One-off data healing [BUG-C3 aftermath]: re-sort nav_history JSON series.

Files written by the old broken ``fetch_missing_nav._date_key`` (months sorted
alphabetically) remain misordered on disk even though every reader now sorts
defensively. This sweeps data/nav_history/*.json, detects non-chronological
series, and rewrites them oldest-first in place (tmp + replace).

Run::

    python scripts/resort_nav_history.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.nav_history import _date_key  # noqa: E402


def main() -> int:
    d = BASE_DIR / "data" / "nav_history"
    if not d.is_dir():
        print(f"no {d}")
        return 1
    scanned = already = fixed = unreadable = 0
    examples: list[str] = []
    for fn in sorted(d.glob("*.json")):
        if not fn.stem.isdigit():  # skip manifest/download_summary sentinels
            continue
        scanned += 1
        try:
            doc = json.loads(fn.read_text(encoding="utf-8"))
            hist = doc["history"]
        except Exception:
            unreadable += 1
            continue
        keys = [_date_key(h.get("date", "")) for h in hist]
        if keys == sorted(keys):
            already += 1
            continue
        doc["history"] = [h for _, h in sorted(zip(hist, keys), key=lambda p: p[1])]
        tmp = fn.with_suffix(".json.resort")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(fn)
        fixed += 1
        if len(examples) < 8:
            examples.append(f"{fn.name}({len(hist)}pts)")
    print(f"scanned={scanned} already_sorted={already} "
          f"resorted={fixed} unreadable={unreadable}")
    if examples:
        print("resorted:", ", ".join(examples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
