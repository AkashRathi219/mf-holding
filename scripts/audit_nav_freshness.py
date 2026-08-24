"""CLI wrapper for the NAV freshness audit — engine lives in src/nav_audit.py.

    python -m scripts.audit_nav_freshness                 # audit + CSV report
    python -m scripts.audit_nav_freshness --sample 10     # bigger live sample
    python -m scripts.audit_nav_freshness --no-live       # offline (no AMFI)
    python -m scripts.audit_nav_freshness --stocks        # include stock files
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.nav_audit import run_audit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=5,
                        help="random schemes to live-verify (default 5)")
    parser.add_argument("--no-live", action="store_true",
                        help="skip the live AMFI correctness sample")
    parser.add_argument("--no-csv", action="store_true",
                        help="skip writing the CSV report")
    parser.add_argument("--stocks", action="store_true",
                        help="also audit stock price files")
    args = parser.parse_args()
    report = run_audit(sample=args.sample, live=not args.no_live,
                       csv_out=None if args.no_csv else "auto",
                       with_stocks=args.stocks)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
