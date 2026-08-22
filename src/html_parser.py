from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def parse_html(html_path: Path) -> dict:
    """Parse a Kotak-style HTML factsheet page into holdings data.

    Kotak's `kotak.bank.in/MF_Factsheet` scheme pages are self-contained HTML
    factsheets whose tables include the top-10 holdings (Company / % of Assets),
    scheme performance, and fund detail ratios.
    """
    result = {
        "source_file": str(html_path),
        "file_type": "html",
        "metadata": {},
        "schemes": {},
        "holdings": [],
        "sectors": [],
    }

    try:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "lxml")
    except Exception as e:
        logger.error(f"HTML parse failed for {html_path}: {e}")
        result["error"] = str(e)
        return result

    text = soup.get_text(" ", strip=True)

    # Metadata
    date_m = re.search(r"Factsheet as on\s+([A-Za-z]+ \d{1,2}, \d{4})", text, re.IGNORECASE)
    if date_m:
        result["metadata"]["date"] = date_m.group(1)

    # Scheme name from <title> or h-fallback
    title = soup.title.get_text(strip=True) if soup.title else ""
    title = re.sub(r"\s*[|\u2013-]\s*Kotak Mahindra Bank.*$", "", title).strip()
    result["metadata"]["scheme_name"] = title

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        header_norm = " ".join(h.lower() for h in header_cells if h)

        if "company" in header_norm or "issuer" in header_norm:
            holdings = []
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue
                holdings.append({
                    "company": cells[0],
                    "percent_nav": _num(cells[1]),
                    "scheme": title,
                })
            if holdings:
                result["holdings"].extend(holdings)
                result["schemes"][title] = {
                    "scheme_name": title,
                    "fund_name": title,
                    "date": result["metadata"].get("date", ""),
                    "holdings": holdings,
                }
            continue

        if "sector" in header_norm and "holdings" in header_norm:
            sectors = []
            for tr in rows[1:]:
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) >= 2:
                    sectors.append({"sector": cells[0], "percent_nav": _num(cells[1])})
            if sectors:
                result["sectors"].extend(sectors)

    return result


def _num(value: str):
    """Convert "6.43" / "7.0" to float, else return the raw string."""
    value = value.replace("%", "").replace(",", "").strip()
    try:
        return float(value)
    except ValueError:
        return value


def save_html_parsed_data(data: dict, output_dir: Path, filename_stem: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{filename_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved JSON: {json_path}")

    rows = []
    for h in data.get("holdings", []):
        row = dict(h)
        row["scheme"] = data.get("metadata", {}).get("scheme_name", "")
        rows.append(row)
    if rows:
        import pandas as pd

        csv_path = output_dir / f"{filename_stem}.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        logger.info(f"Saved CSV: {csv_path} ({len(rows)} rows)")
