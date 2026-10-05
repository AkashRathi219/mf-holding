"""Export the AMC -> document link inventory as a clickable workbook/CSV.

Reads every ``data/logs/portfolio_links/*.json`` manifest (all months pulled),
checks the downloaded file under ``data/raw/pdfs/`` and its parsed output, and
writes:

    output/amc_document_links.xlsx
        sheet "Document links" — every document, URL cell hyperlinked
        sheet "AMC link sheet"  — all 57 registry AMCs in column A; column B is
                                 left empty for the operator to paste each AMC's
                                 portfolio/factsheet page URL
    output/amc_document_links.csv       same rows as sheet 1 (CSV fallback)
    output/amc_summary.csv              one row per AMC (counts + latest month)

Run::

    python scripts/export_document_links_csv.py
    python scripts/export_document_links_csv.py --months 2026-09 2026-10
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
LINKS_DIR = BASE_DIR / "data" / "logs" / "portfolio_links"
PDFS = BASE_DIR / "data" / "raw" / "pdfs"
PARSED = BASE_DIR / "data" / "parsed" / "amc_websites"
OUT_DIR = BASE_DIR / "output"
CSV_OUT = OUT_DIR / "amc_document_links.csv"
SUMMARY_OUT = OUT_DIR / "amc_summary.csv"
XLSX_OUT = OUT_DIR / "amc_document_links.xlsx"


def _safe(name: str) -> str:
    return name.replace(" ", "_").replace("/", "-")


def build(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    for m in months:
        path = LINKS_DIR / f"{m}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for amc in doc.get("amcs", []):
            name = amc["mf_name"]
            if not (amc.get("links") or []):
                continue
            for link in amc.get("links", []) or []:
                y, mo = link.get("disclosure_year"), link.get("disclosure_month")
                if y is None or mo is None:
                    continue
                fname = link["filename"]
                raw = PDFS / _safe(name) / str(y) / f"{mo:02d}" / fname
                parsed = PARSED / _safe(name) / str(y) / f"{mo:02d}" / f"{Path(fname).stem}.json"
                rows.append({
                    "amc": name,
                    "disclosure_month": f"{y}-{mo:02d}",
                    "document_type": link.get("document_type") or "",
                    "filename": fname,
                    "download_url": link.get("url") or "",
                    "downloaded": "yes" if raw.exists() else "no",
                    "size_kb": round(raw.stat().st_size / 1024, 1) if raw.exists() else "",
                    "parsed": "yes" if parsed.exists() else "no",
                    "local_path": (raw.relative_to(BASE_DIR).as_posix()
                                   if raw.exists() else ""),
                })
    rows.sort(key=lambda r: (r["amc"], r["disclosure_month"], r["filename"]), reverse=True)
    return rows


def summary(rows: list[dict]) -> list[dict]:
    per: dict[str, dict] = defaultdict(
        lambda: {"documents": 0, "downloaded": 0, "parsed": 0, "months": set(),
                 "latest_month": "", "links": set()})
    for r in rows:
        rec = per[r["amc"]]
        rec["documents"] += 1
        rec["downloaded"] += r["downloaded"] == "yes"
        rec["parsed"] += r["parsed"] == "yes"
        rec["months"].add(r["disclosure_month"])
        rec["latest_month"] = max(rec["latest_month"], r["disclosure_month"])
        if r["download_url"]:
            rec["links"].add(r["download_url"])

    out = []
    for amc in sorted(per):
        rec = per[amc]
        links = sorted(rec["links"])
        out.append({
            "amc": amc,
            "documents": rec["documents"],
            "downloaded": rec["downloaded"],
            "parsed": rec["parsed"],
            "months_available": ", ".join(sorted(rec["months"], reverse=True)),
            "latest_month": rec["latest_month"],
            "document_links": " | ".join(links),
        })

    # Registry AMCs whose adapters found nothing: listed explicitly so the CSV
    # is the complete AMC roster, not just the ones that happened to link.
    registry = BASE_DIR / "config" / "amc_registry.json"
    have = {r["amc"] for r in out}
    if registry.exists():
        amcs = json.loads(registry.read_text(encoding="utf-8-sig"))
        for a in amcs:
            name = a.get("mf_name") or ""
            if name in have:
                continue
            out.append({
                "amc": name,
                "documents": 0, "downloaded": 0, "parsed": 0,
                "months_available": "", "latest_month": "",
                "document_links": "",
            })
    return sorted(out, key=lambda r: r["amc"].lower())


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _amc_link_template() -> list[str]:
    """All registry AMC names, alphabetically — column A of the input sheet."""
    registry = BASE_DIR / "config" / "amc_registry.json"
    names = []
    if registry.exists():
        amcs = json.loads(registry.read_text(encoding="utf-8-sig"))
        names = sorted((a.get("mf_name") or "").strip() for a in amcs
                       if (a.get("mf_name") or "").strip())
    return names


def write_xlsx(path: Path, rows: list[dict], summ: list[dict]) -> bool:
    """Two-sheet workbook: hyperlinked document inventory + a blank AMC link
    sheet (column A = AMC, column B = operator-supplied URL). False when
    openpyxl is unavailable."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False

    wb = Workbook()
    ws = wb.active
    ws.title = "Document links"

    headers = list(rows[0].keys())
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")
    url_col = headers.index("download_url") + 1
    for r in rows:
        ws.append([r[h] for h in headers])
        cell = ws.cell(row=ws.max_row, column=url_col)
        if r["download_url"]:
            cell.hyperlink = r["download_url"]
            cell.style = "Hyperlink"
    ws.freeze_panes = "A2"
    widths = {"amc": 34, "disclosure_month": 16, "document_type": 18,
              "filename": 60, "download_url": 70, "downloaded": 11,
              "size_kb": 9, "parsed": 8, "local_path": 55}
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 16)

    summary_ws = wb.create_sheet("AMC summary")
    sh = list(summ[0].keys())
    summary_ws.append(sh)
    for cell in summary_ws[1]:
        cell.font = Font(bold=True)
    for r in summ:
        summary_ws.append([r[k] for k in sh])
    summary_ws.freeze_panes = "A2"
    for i, h in enumerate(sh, 1):
        summary_ws.column_dimensions[get_column_letter(i)].width = (
            60 if h == "document_links" else 18)

    # Column A = every AMC, column B = blank for the operator to paste links.
    inp = wb.create_sheet("AMC link sheet")
    inp.append(["amc", "portfolio_factsheet_link (fill in)"])
    for cell in inp[1]:
        cell.font = Font(bold=True)
    for name in _amc_link_template():
        inp.append([name, ""])
    inp.column_dimensions["A"].width = 40
    inp.column_dimensions["B"].width = 70
    inp.freeze_panes = "A2"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", default=None)
    args = ap.parse_args()
    months = args.months or sorted(p.stem for p in LINKS_DIR.glob("*.json"))
    rows = build(months)
    if not rows:
        print("no manifests found")
        return 1
    write_csv(CSV_OUT, rows)
    summ = summary(rows)
    write_csv(SUMMARY_OUT, summ)
    print(f"{len(rows)} documents across {len(summ)} AMCs")
    print(f"-> {CSV_OUT}")
    print(f"-> {SUMMARY_OUT}")
    if write_xlsx(XLSX_OUT, rows, summ):
        print(f"-> {XLSX_OUT} "
              f"(sheets: Document links / AMC summary / AMC link sheet "
              f"- {len(_amc_link_template())} AMCs in column A)")
    else:
        print("openpyxl unavailable - skipped workbook (CSVs written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
