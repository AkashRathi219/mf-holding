"""Sweep every stock_financials doc through build_annual_table and aggregate
the arithmetic-consistency checks, multi-year metric coverage and warnings.

The Statements tab ([fund-table-v1.1.0]) renders filed annual statements as
rows with audited fiscal years as columns. This tool answers:
  * how many docs produce a table at all, and how many fiscal years each has;
  * which consistency checks pass / fail / are not applicable across the corpus;
  * the most common honest-gap warnings (missing balance-sheet / cash-flow,
    loss years, single-year docs);
  * any doc where a check fails or the table throws.

Usage:
    python scripts/audit_fundamental_table.py            # report only
    python scripts/audit_fundamental_table.py --json     # machine-readable
    python scripts/audit_fundamental_table.py --bad-only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.stock_fundamental import (  # noqa: E402
    FUND_TABLE_VERSION,
    build_annual_table,
)

DOC_DIR = ROOT / "data" / "stock_financials"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--bad-only", action="store_true",
                    help="only print docs with failed checks or exceptions")
    args = ap.parse_args()

    docs = sorted(DOC_DIR.glob("*.json"))
    results, errors = [], []
    for path in docs:
        isin = path.stem
        try:
            table = build_annual_table(json.loads(path.read_text(encoding="utf-8")))
            results.append((isin, table))
        except Exception as exc:  # noqa: BLE001 - report and keep sweeping
            errors.append((isin, f"{type(exc).__name__}: {exc}"))

    checks = Counter()
    warnings = Counter()
    years_n = Counter()
    failed = []

    for isin, t in results:
        if not t.get("available"):
            checks["unavailable"] += 1
            years_n["n/a"] += 1
            continue
        years_n[t["years_n"]] += 1
        for key, val in (t.get("checks") or {}).items():
            label = "fail" if val is False else ("pass" if val is True else "n/a")
            checks[f"{key}={label}"] += 1
            if val is False:
                failed.append(f"{isin}: {key}")
        for w in t.get("warnings") or []:
            short = w.split(":", 1)[0].rstrip(" .")
            warnings[short] += 1

    summary = {
        "table_version": FUND_TABLE_VERSION,
        "docs": len(docs),
        "docs_with_errors": len(errors),
        "available": sum(1 for _, t in results if t.get("available")),
        "years_n": dict(sorted(years_n.items(), key=lambda kv: str(kv[0]))),
        "checks": dict(checks.most_common()),
        "failed_checks": failed,
        "warnings": dict(warnings.most_common(25)),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"fund table audit · {FUND_TABLE_VERSION} · {len(docs)} docs")
    print(f"  errors while building: {len(errors)}")
    for isin, msg in errors:
        print(f"    ERROR {isin}: {msg}")
    print(f"  available: {summary['available']}  unavailable: "
          f"{summary['docs'] - summary['available']}")
    print("  fiscal years per doc: "
          + ", ".join(f"n={k}:{v}" for k, v in sorted(years_n.items(), key=lambda kv: str(kv[0]))))
    print("  consistency checks:")
    for k, v in checks.most_common():
        print(f"    {k}: {v}")
    if failed:
        print(f"  docs with a failing check ({len(failed)}):")
        for f in failed:
            print(f"    {f}")
    print("  top warnings:")
    for k, v in warnings.most_common(15):
        print(f"    {v:>5}  {k}")
    if args.bad_only:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())