"""Export the latest factsheet/portfolio document index for the frontend.

Scans data/logs/portfolio_links/*.json and data/raw/pdfs/ to produce
public/data/factsheets.json in the welcome-gateway frontend. The frontend
renders the last N months of downloadable factsheet/portfolio documents
per AMC, with the latest month called out.

Usage:
    python scripts/export_factsheet_index.py [--months-back 3]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LINKS_DIR = BASE / "data" / "logs" / "portfolio_links"
PDFS = BASE / "data" / "raw" / "pdfs"
OUT = BASE.parent / "mfholding_frontend" / "welcome-gateway" / "public" / "data" / "factsheets.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months-back", type=int, default=3)
    args = ap.parse_args()

    manifests = sorted(LINKS_DIR.glob("*.json"))
    manifests = manifests[-args.months_back:]

    by_amc: dict[str, dict] = {}
    month_counts: dict[str, int] = defaultdict(int)

    for mpath in manifests:
        doc = json.loads(mpath.read_text(encoding="utf-8"))
        for amc in doc.get("amcs", []):
            name = amc["mf_name"]
            row = by_amc.setdefault(name, {"amc": name, "documents": []})
            for link in amc.get("links", []) or []:
                y, mo = link.get("disclosure_year"), link.get("disclosure_month")
                if y is None or mo is None:
                    continue
                safe = name.replace(" ", "_").replace("/", "-")
                local = PDFS / safe / str(y) / f"{mo:02d}" / link["filename"]
                row["documents"].append({
                    "filename": link["filename"],
                    "url": link.get("url"),
                    "type": link.get("document_type"),
                    "month": f"{y}-{mo:02d}",
                    "available_locally": local.exists(),
                })
                month_counts[f"{y}-{mo:02d}"] += 1

    for row in by_amc.values():
        row["documents"].sort(key=lambda d: (d["month"], d["filename"]), reverse=True)
        row["latest_month"] = max((d["month"] for d in row["documents"]), default=None)
        row["count"] = len(row["documents"])

    months_sorted = sorted(month_counts, reverse=True)
    payload = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "latest_months": months_sorted,
        "total_documents": sum(month_counts.values()),
        "amcs": sorted(by_amc.values(), key=lambda r: r["amc"]),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT} — {payload['total_documents']} docs, "
          f"{len(payload['amcs'])} AMCs, months {months_sorted}")


if __name__ == "__main__":
    main()
