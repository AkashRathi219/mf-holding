"""Combined stock refresh: identity -> price (daily) -> actions -> reports.

Run::

    python -m src.stock_refresh
"""

from __future__ import annotations

import sys

from .stock_actions import run as run_actions
from .stock_identity import build_identity
from .stock_price import run as run_price
from .stock_reports import run as run_reports


def refresh_all(daily: bool = True, limit: int | None = None) -> dict:
    from .refresh_log import track
    with track("stock_refresh", daily=daily, limit=limit) as _meta:
        summary = _refresh_all_impl(daily=daily, limit=limit)
        _meta.update(summary)
        return summary


def _refresh_all_impl(daily: bool = True, limit: int | None = None) -> dict:
    print("  [1/4] identity…", flush=True)
    ident = build_identity(force=False)
    print(f"        {len(ident)} ISINs", flush=True)
    print("  [2/4] prices (bhavcopy -> Yahoo -> Google)…", flush=True)
    price = run_price(ident=ident, daily=daily, limit=limit)
    print(f"        ok={sum(1 for r in price if r.get('status') == 'ok')}", flush=True)
    print("  [3/4] corporate actions…", flush=True)
    actions = run_actions(ident=ident, limit=limit)
    print(f"        ok={sum(1 for r in actions if r.get('status') == 'ok')}", flush=True)
    print("  [4/4] NSE financial-report announcements…", flush=True)
    reports = run_reports(ident=ident, limit=limit)
    print(f"        ok={sum(1 for r in reports if r.get('status') == 'ok')}", flush=True)
    return {
        "identity": len(ident),
        "price_ok": sum(1 for r in price if r.get("status") == "ok"),
        "actions_ok": sum(1 for r in actions if r.get("status") == "ok"),
        "reports_ok": sum(1 for r in reports if r.get("status") == "ok"),
    }


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--full", action="store_true", help="full backfill (not incremental)")
    args = parser.parse_args()
    summary = refresh_all(daily=not args.full, limit=args.limit)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())