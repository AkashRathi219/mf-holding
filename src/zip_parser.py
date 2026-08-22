"""ZIP-archive monthly-portfolio parsing.

Several AMCs publish their monthly portfolio holdings as a ZIP archive
containing one file per scheme (XLSX/PDF/CSV).  This module extracts such an
archive into a temporary directory, parses every supported member document and
aggregates the results into a single parsed dict whose ``schemes`` key is keyed
by scheme name - matching the shape produced by the Excel / grouped-PDF
parsers so downstream consumers (report, all_schemes.csv) count every scheme.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from src.excel_parser import parse_excel
from src.pdf_parser import parse_pdf
from src.html_parser import parse_html

logger = logging.getLogger(__name__)

_SUPPORTED_MEMBERS = (".pdf", ".xlsx", ".xls", ".csv", ".html")


def _parse_member(path: Path) -> dict | None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix in (".xlsx", ".xls"):
        return parse_excel(path)
    if suffix == ".html":
        return parse_html(path)
    if suffix == ".csv":
        try:
            import pandas as pd

            df = pd.read_csv(path)
            schemes = {}
            name = path.stem
            schemes[name] = {
                "scheme_name": name,
                "fund_name": name,
                "date": "",
                "holdings": df.to_dict(orient="records"),
                "sectors": [],
            }
            return {
                "source_file": str(path),
                "file_type": "csv",
                "metadata": {"total_schemes": 1},
                "schemes": schemes,
            }
        except Exception as e:
            logger.warning(f"CSV member parse failed {path.name}: {e}")
            return None
    return None


def _merge_schemes(target: dict, member: dict, member_name: str) -> None:
    """Absorb *member*'s schemes (and holdings) into *target*."""
    schemes = member.get("schemes")
    if isinstance(schemes, dict) and schemes:
        for code, scheme in schemes.items():
            if not isinstance(scheme, dict):
                continue
            name = scheme.get("fund_name") or scheme.get("scheme_name") or str(code)
            if not name or name.lower() in ("sheet1", code.lower(), "sheet"):
                # Generic sheet code - the member filename carries the scheme name.
                name = member_name
            holdings = scheme.get("holdings") or []
            if not holdings and isinstance(member.get("equity_holdings"), list):
                for table in member["equity_holdings"]:
                    holdings.extend(_table_rows(table))
            target.setdefault(name, {
                "scheme_name": name,
                "fund_name": name,
                "date": scheme.get("date", ""),
                "holdings": [],
                "sectors": [],
            })
            existing = target[name]
            for h in holdings:
                if isinstance(h, dict):
                    existing["holdings"].append(h)
            if not existing["date"]:
                existing["date"] = scheme.get("date", "")
    else:
        # PDF-style flat document (equity/debt holdings, no schemes dict).
        name = member_name
        holdings = []
        for key in ("equity_holdings", "debt_holdings", "top_holdings"):
            for table in member.get(key) or []:
                holdings.extend(_table_rows(table))
        if holdings:
            target.setdefault(name, {
                "scheme_name": name,
                "fund_name": name,
                "date": (member.get("metadata") or {}).get("date", ""),
                "holdings": [],
                "sectors": [],
            })
            target[name]["holdings"].extend(holdings)


def _table_rows(table) -> list[dict]:
    """Flatten a table (list-of-dicts or dict-with-rows) into row dicts."""
    if isinstance(table, dict):
        return table.get("rows", [])
    if isinstance(table, list):
        return table
    return []


def parse_zip(zip_path: Path) -> dict:
    """Extract a ZIP archive and parse every supported member into schemes.

    Returns a single parsed dict with a merged ``schemes`` mapping keyed by
    scheme name.
    """
    zip_path = Path(zip_path)
    result = {
        "source_file": str(zip_path),
        "file_type": "zip",
        "metadata": {"archive_members": 0},
        "schemes": {},
        "amc_name": None,
    }

    tmpdir = Path(tempfile.mkdtemp(prefix="mf_zip_"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                zf.extractall(tmpdir)
            except (zipfile.BadZipFile, RuntimeError, OSError) as e:
                logger.error(f"Bad ZIP {zip_path}: {e}")
                result["error"] = str(e)
                return result

        members = sorted(
            p for p in tmpdir.rglob("*")
            if p.is_file() and p.suffix.lower() in _SUPPORTED_MEMBERS
        )
        result["metadata"]["archive_members"] = len(members)
        for member in members:
            parsed = _parse_member(member)
            if not parsed:
                continue
            _merge_schemes(result["schemes"], parsed, member.stem)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    n = len(result.get("schemes", {}))
    logger.info(
        f"ZIP {zip_path.name}: {result['metadata']['archive_members']} members "
        f"-> {n} schemes"
    )
    return result


def save_zip_parsed_data(data: dict, output_dir: Path, filename_stem: str) -> None:
    """Persist a ZIP-parsed result as JSON + a per-scheme CSV."""
    import json

    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{filename_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved JSON: {json_path}")

    all_holdings = []
    for name, scheme in (data.get("schemes") or {}).items():
        for holding in scheme.get("holdings", []):
            if isinstance(holding, dict):
                row = dict(holding)
                row.setdefault("scheme", name)
                all_holdings.append(row)

    if all_holdings:
        import pandas as pd

        csv_path = output_dir / f"{filename_stem}.csv"
        pd.DataFrame(all_holdings).to_csv(csv_path, index=False)
        logger.info(f"Saved CSV: {csv_path} ({len(all_holdings)} rows)")
