"""Backfill-status reporter for the stock agents.

Scans the resolved stock universe (``data/stocks/identity.json``) and reports how
much of the price / corporate-action / report backfill has completed::

    python -m src.stock_status                # human table
    python -m src.stock_status --json         # machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .stock_common import (ACTIONS_DIR, HISTORY_DIR, IDENTITY_JSON, REPORTS_DIR,
                           load_json)
from .stock_identity import load_identity


def report() -> dict:
    ident = load_identity()
    total = len(ident)
    total_with_symbol = sum(1 for v in ident.values() if v.get("symbol"))

    def _count(directory: Path) -> int:
        return len(list(directory.glob("*.json"))) if directory.is_dir() else 0

    price_files = _count(HISTORY_DIR)
    actions_files = _count(ACTIONS_DIR)
    reports_files = _count(REPORTS_DIR)

    # Freshness of price data (max last date across completed price files).
    latest_date = ""
    total_points = 0
    if HISTORY_DIR.is_dir():
        for p in HISTORY_DIR.glob("*.json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            hist = doc.get("history") or []
            total_points += len(hist)
            if hist and (hist[-1].get("date") or "") > latest_date:
                latest_date = hist[-1].get("date") or ""

    pct = lambda n: round(n / total * 100, 1) if total else 0.0  # noqa: E731
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_stocks": total,
        "with_symbol": total_with_symbol,
        "price_done": price_files,
        "price_pct": pct(price_files),
        "actions_done": actions_files,
        "actions_pct": pct(actions_files),
        "reports_done": reports_files,
        "reports_pct": pct(reports_files),
        "price_points": total_points,
        "price_latest_date": latest_date,
        "missing": {
            "price": sorted(
                isin for isin, v in ident.items()
                if not (HISTORY_DIR / f"{isin}.json").exists()),
            "actions": sorted(
                isin for isin, v in ident.items()
                if not (ACTIONS_DIR / f"{isin}.json").exists()),
            "reports": sorted(
                isin for isin, v in ident.items()
                if not (REPORTS_DIR / f"{isin}.json").exists()),
        },
    }


def _print_table(r: dict) -> None:
    print("=" * 60)
    print("STOCK BACKFILL STATUS")
    print("=" * 60)
    print(f"  Universe (confirmed-equity): {r['total_stocks']} "
          f"(NSE symbols: {r['with_symbol']})")
    print(f"  Price history : {r['price_done']}/{r['total_stocks']} "
          f"({r['price_pct']}%)  -> {r['price_points']} pts, latest {r['price_latest_date'] or '—'}")
    print(f"  Corp actions  : {r['actions_done']}/{r['total_stocks']} ({r['actions_pct']}%)")
    print(f"  Reports       : {r['reports_done']}/{r['total_stocks']} ({r['reports_pct']}%)")
    print("-" * 60)
    for key, label in (("price", "Price"), ("actions", "Actions"), ("reports", "Reports")):
        missing = r["missing"][key]
        if missing:
            shown = ", ".join(missing[:10])
            more = f" … +{len(missing) - 10} more" if len(missing) > 10 else ""
            print(f"  Missing {label}: {shown}{more}")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="output JSON only")
    args = parser.parse_args()
    r = report()
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        _print_table(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())