"""Statement coverage report — audited/unaudited quarterly + annual filings
for the last five fiscal years across every tracked equity.

Writes:
  output/statements_coverage.csv   long format (symbol x FY x period x audit)
  output/statements_coverage.md    human-readable summary + missing lists

Usage:
    python scripts/statement_coverage_report.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.statement_coverage import (  # noqa: E402
    AUDITS, build_coverage, csv_rows)

OUT_DIR = Path("output")
CSV_PATH = OUT_DIR / "statements_coverage.csv"
MD_PATH = OUT_DIR / "statements_coverage.md"


def refresh_outputs() -> None:
    """Regenerate the CSV + Markdown report (also called by the wave
    coordinator after every wave)."""
    payload = build_coverage()
    header, rows = csv_rows(payload)
    OUT_DIR.mkdir(exist_ok=True)
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    MD_PATH.write_text(_markdown(payload), encoding="utf-8")
    print(f"coverage: {payload['with_doc']}/{payload['universe']} docs, "
          f"{len(rows)} filled cells -> {CSV_PATH} / {MD_PATH}")


def _markdown(payload: dict) -> str:
    fys = payload["window_fys"]
    lines = [
        "# Statement coverage — audited/unaudited filings",
        "",
        f"Generated: {payload['generated_at']} · universe "
        f"{payload['universe']} tracked equities · "
        f"{payload['with_doc']} with parsed statements · window "
        f"{'–'.join(fys)}",
        "",
        "## Parsed annual filings per FY",
        "",
        "| FY | Audited | Unaudited | unknown |",
        "|---|---|---|---|",
    ]
    for fy in fys:
        counts = [payload["summary"].get(f"{fy}|FY|{a}", 0)
                  for a in AUDITS]
        lines.append(f"| {fy} | {counts[0]} | {counts[1]} | {counts[2]} |")
    lines += ["", "## Parsed discrete quarters per FY (Q1-Q4)",
              "", "| FY | Audit | Q1 | Q2 | Q3 | Q4 |", "|---|---|---|---|---|---|"]
    for fy in fys:
        for audit in ("Audited", "Unaudited", "unknown"):
            counts = [payload["summary"].get(f"{fy}|{q}|{audit}", 0)
                      for q in ("Q1", "Q2", "Q3", "Q4")]
            lines.append(
                f"| {fy} | {audit} | {counts[0]} | {counts[1]} | "
                f"{counts[2]} | {counts[3]} |")
    # filed-but-not-parsed (drives the next batches)
    missing_annual: list[str] = []
    for s in payload["stocks"]:
        for fy in fys:
            if not s["parsed"].get(f"{fy}|FY|Audited") \
                    and s["filed"].get(f"{fy}|FY|Audited"):
                missing_annual.append(f"{s['symbol']} ({fy})")
    if missing_annual:
        lines += ["", "## Filed (audited) but not yet parsed",
                  "", ", ".join(missing_annual)]
    no_doc = [s["symbol"] or s["isin"] for s in payload["stocks"]
              if not s["doc_available"]]
    if no_doc:
        lines += ["", f"## No parsed statements document ({len(no_doc)})",
                  "", ", ".join(no_doc)]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    refresh_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
