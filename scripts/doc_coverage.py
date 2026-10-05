"""Document coverage gap report: AMC-offered links (portfolio_links manifests)
vs local raw files vs parsed JSON output.

Usage:
    python scripts/doc_coverage.py [--months YYYY-MM YYYY-MM ...]

Writes:
    data/reports/doc_coverage_<YYYY-MM-DD>.json
    data/reports/doc_coverage_<YYYY-MM-DD>.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

PDFS = BASE / "data" / "raw" / "pdfs"
PARSED = BASE / "data" / "parsed" / "amc_websites"
LINKS_DIR = BASE / "data" / "logs" / "portfolio_links"
OUT_DIR = BASE / "data" / "reports"


def _safe_amc(name: str) -> str:
    return name.replace(" ", "_").replace("/", "-")


def build(months: list[str]) -> dict:
    per_amc: dict[str, dict] = {}
    totals = {"links": 0, "raw_present": 0, "parsed_present": 0,
              "missing_raw": 0, "missing_parsed": 0}
    missing = []

    for m in months:
        path = LINKS_DIR / f"{m}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for amc in doc.get("amcs", []):
            name = amc["mf_name"]
            row = per_amc.setdefault(name, {
                "links": 0, "raw_present": 0, "parsed_present": 0,
                "missing_raw": [], "missing_parsed": []})
            for link in amc.get("links", []) or []:
                totals["links"] += 1
                row["links"] += 1
                y, mo = link.get("disclosure_year"), link.get("disclosure_month")
                if y is None or mo is None:
                    continue
                raw = PDFS / _safe_amc(name) / str(y) / f"{mo:02d}" / link["filename"]
                stem = Path(link["filename"]).stem
                parsed = PARSED / _safe_amc(name) / str(y) / f"{mo:02d}" / f"{stem}.json"
                if raw.exists():
                    totals["raw_present"] += 1
                    row["raw_present"] += 1
                else:
                    totals["missing_raw"] += 1
                    row["missing_raw"].append(link["filename"])
                    missing.append({"amc": name, "file": link["filename"],
                                    "month": f"{y}-{mo:02d}", "kind": "raw"})
                if parsed.exists():
                    totals["parsed_present"] += 1
                    row["parsed_present"] += 1
                else:
                    totals["missing_parsed"] += 1
                    row["missing_parsed"].append(link["filename"])
                    missing.append({"amc": name, "file": link["filename"],
                                    "month": f"{y}-{mo:02d}", "kind": "parsed"})

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "months": months,
        "totals": totals,
        "per_amc": per_amc,
        "missing": missing,
    }


def _markdown(payload: dict) -> str:
    t = payload["totals"]
    lines = [
        "# Document coverage — factsheets & portfolios",
        "",
        f"Generated: {payload['generated_at']} · months: {', '.join(payload['months'])}",
        "",
        f"- Offered links: {t['links']}",
        f"- Raw present: {t['raw_present']} (missing {t['missing_raw']})",
        f"- Parsed present: {t['parsed_present']} (missing {t['missing_parsed']})",
        "",
        "## Per-AMC",
        "",
        "| AMC | links | raw | parsed | missing raw | missing parsed |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(payload["per_amc"]):
        r = payload["per_amc"][name]
        lines.append(f"| {name} | {r['links']} | {r['raw_present']} | "
                     f"{r['parsed_present']} | {len(r['missing_raw'])} | "
                     f"{len(r['missing_parsed'])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", default=None)
    args = ap.parse_args()
    if args.months:
        months = args.months
    else:
        months = sorted(p.stem for p in LINKS_DIR.glob("*.json"))
    payload = build(months)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    json_path = OUT_DIR / f"doc_coverage_{stamp}.json"
    md_path = OUT_DIR / f"doc_coverage_{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown(payload), encoding="utf-8")
    t = payload["totals"]
    print(f"links={t['links']} raw_present={t['raw_present']} "
          f"parsed_present={t['parsed_present']} missing_raw={t['missing_raw']} "
          f"missing_parsed={t['missing_parsed']}")
    print(f"-> {json_path}\n-> {md_path}")


if __name__ == "__main__":
    main()
