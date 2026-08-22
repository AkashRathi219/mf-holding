from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SEBI_COLUMNS = {
    "company": ["name of the instrument", "issuer", "company", "stock", "security", "instrument"],
    "isin": ["isin"],
    "sector": ["industry", "sector", "rating / industry"],
    "instrument": ["instrument type", "type of instrument"],
    "rating": ["rating", "credit rating", "grade"],
    "quantity": ["quantity", "qty", "no of shares", "face value"],
    "market_value": ["market value", "market/fair value", "mkt value", "fair value", "market value (rs"],
    "percent_nav": ["% to nav", "% to aum", "% of nav", "% nav", "percentage", "% to net assets", "weightage"],
    "yield": ["yield", "ytm", "ytc", "yield to maturity"],
}

SECTION_KEYWORDS = {
    "equity", "debt", "money market", "equity & equity related",
    "debt instruments", "government securities", "treasury bills",
    "commercial paper", "certificate of deposit", "non convertible debentures",
    "current assets", "net current assets", "cash and cash equivalents",
    "reverse repo", "reverse repo / treps", "treps", "tri party repo",
    "net receivables", "net receivables / (payables)", "short term deposit",
    "short term deposits", "bank deposit", "cash equivalents",
}

_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _num(value) -> float | None:
    """Parse a numeric cell (handles lakhs commas etc.)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("₹", "").strip()
    if not s or s.lower() in ("na", "n/a", "-", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_valid_isin(value: str) -> bool:
    """A genuine ISIN is 12 chars: 2-letter country code + 9 alphanumerics + check digit."""
    return bool(value and _ISIN_PATTERN.match(value.strip().upper()))


def parse_excel(excel_path: Path) -> dict:
    """Parse an Excel file containing portfolio holdings data."""
    result = {
        "source_file": str(excel_path),
        "file_type": "excel",
        "metadata": {},
        "schemes": {},
    }

    try:
        xl = pd.ExcelFile(excel_path)
        logger.info(f"Excel file has {len(xl.sheet_names)} sheets: {xl.sheet_names[:5]}")

        index_sheet = None
        for name in xl.sheet_names:
            if name.lower() in ("index", "contents", "summary"):
                index_sheet = name
                break

        for sheet_name in xl.sheet_names:
            if sheet_name == index_sheet:
                continue

            df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)
            if df.empty or len(df) < 3:
                continue

            parsed_sheet = _parse_sheet(df, sheet_name)
            if parsed_sheet and parsed_sheet.get("holdings"):
                result["schemes"][sheet_name] = parsed_sheet

        if result["schemes"]:
            first_scheme = next(iter(result["schemes"].values()))
            if first_scheme.get("holdings"):
                result["metadata"]["sample_scheme"] = first_scheme.get("scheme_name", "")
                result["metadata"]["total_schemes"] = len(result["schemes"])

    except Exception as e:
        logger.error(f"Error parsing Excel {excel_path}: {e}")
        result["error"] = str(e)

    return result


def _parse_sheet(df: pd.DataFrame, sheet_name: str) -> dict:
    """Parse a single sheet that may contain portfolio data."""
    result = {
        "scheme_name": sheet_name,
        "fund_name": "",
        "date": "",
        "holdings": [],
        "sectors": [],
    }

    _extract_metadata(df, result)

    header_row = _find_header_row(df)
    if header_row is None:
        return result

    headers = []
    for v in df.iloc[header_row]:
        if pd.notna(v):
            headers.append(re.sub(r"\s+", " ", str(v).strip().lower()))
        else:
            headers.append("")

    col_map = _map_columns(headers)

    data_start = header_row + 1
    current_section = ""

    company_col = col_map.get("company", [])
    company_idx = company_col[0] if company_col else None

    for idx in range(data_start, len(df)):
        row = df.iloc[idx]
        row_vals = [str(v).strip() if pd.notna(v) else "" for v in row]

        if all(v == "" for v in row_vals):
            continue

        non_empty_count = sum(1 for v in row_vals if v)
        company_val = row_vals[company_idx] if company_idx is not None and company_idx < len(row_vals) else ""
        company_lower = company_val.lower().strip()

        # Section/sub-section headers (e.g. "EQUITY & EQUITY RELATED") occupy only
        # a few cells in the mapped company column and are never holdings.
        if company_val and non_empty_count <= 3 and (
            company_lower in SECTION_KEYWORDS
            or "total" in company_lower
            or (company_val.isupper()
                and len(company_val) > 3
                and not any(c.isdigit() for c in company_val))
        ):
            current_section = company_val.strip()
            continue

        holding = {}
        for standard_name, col_indices in col_map.items():
            if col_indices and col_indices[0] < len(row_vals):
                val = row_vals[col_indices[0]]
                holding[standard_name] = val

        if _is_valid_isin(holding.get("isin", "")) or (holding.get("company") and holding.get("quantity")):
            holding["section"] = current_section
            result["holdings"].append(holding)
        elif holding.get("company") and _num(holding.get("market_value")) is not None:
            # Cash-equivalent rows (Reverse Repo / TREPS / Net Receivables) carry a
            # market value but no quantity/ISIN; keep them so cash exposure is shown.
            holding["section"] = current_section
            result["holdings"].append(holding)

    return result


def _extract_metadata(df: pd.DataFrame, result: dict) -> None:
    """Extract scheme metadata from top rows."""
    for i in range(min(15, len(df))):
        row_vals = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[i]]
        row_text = " ".join(row_vals).upper()

        if "SCHEME NAME" in row_text:
            for v in row_vals:
                if v and "SCHEME NAME" not in v:
                    result["fund_name"] = v.strip()
                    break

        if "PORTFOLIO STATEMENT" in row_text or "AS ON" in row_text:
            for v in row_vals:
                if v and "PORTFOLIO" not in v and "AS ON" not in v:
                    result["date"] = v.strip()
                    break

        # Union-style: "MONTHLY PORTFOLIO STATEMENT OF UNION AGGRESSIVE HYBRID
        # FUND AS ON JULY 31, 2026" -> scheme name between "OF" and "AS ON".
        if not result["fund_name"]:
            m = re.search(r"PORTFOLIO STATEMENT OF (.+?) AS ON ", row_text)
            if m and len(m.group(1).strip()) >= 4:
                result["fund_name"] = m.group(1).strip()
        # Kotak-style: "Portfolio of Kotak Nifty India Tourism Index Fund as on
        # 31-Jul-2026" -> scheme name between "Portfolio of" and "as on".
        if not result["fund_name"]:
            m = re.search(r"PORTFOLIO OF (.+?) AS ON ", row_text)
            if m and len(m.group(1).strip()) >= 4:
                result["fund_name"] = m.group(1).strip()

    # UTI-style layout: row 0 is the AMC brand ("UTI MUTUAL FUND"), row 1 is the
    # scheme name ("UTI - Transportation and Logistics Fund").  No "SCHEME NAME"
    # label exists, so recover the first scheme-looking line.  A 1-token code
    # ("MMF01", "YB01", "AS") is a sheet code, not a name - require 2+ words.
    if not result["fund_name"]:
        _BLOCK = (
            "portfolio", "provisional", "unaudited", "disclosure",
            "as of", "as on", "name of the instrument", "% to", "isin",
            "quantity", "rating", "scheme code", "amc", "product labelling",
            "risk-o-meter", "riskometer", "market/fair value", "market value",
            "name of mutual fund", "yield", "ytm", "coupon",
        )
        for i in range(min(10, len(df))):
            row_vals = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[i]]
            for v in row_vals:
                cand = v.strip()
                # Strip the descriptive suffix first so the length gate is
                # applied to the bare scheme name, not the whole title cell
                # ("Bank of India Ultra Short Duration Fund (An open ended
                # ultra-short term debt scheme ...)" is >200 chars).
                bare = re.sub(r"\(\s*(?:An|A)\s+Open[- ]Ended.*$", "", cand, flags=re.I).strip()
                bare = re.sub(r"\s*-\s*(?:An|A)\s+Open[- ]Ended.*$", "", bare, flags=re.I).strip()
                bare = re.sub(r"\s+-\s*$", "", bare).strip()
                low = bare.lower()
                if (3 < len(bare) < 200 and len(bare.split()) >= 2
                        and not _is_valid_isin(bare)
                        and not _looks_like_brand_line(low)
                        and not any(k in low for k in _BLOCK)):
                    # Prefer a cell that clearly names a scheme ("... Fund",
                    # "... Yojana", "... ETF") over generic header fragments.
                    if re.search(r"\b(fund|scheme|yojana|etf|fof|index fund)\b", low, re.I):
                        result["fund_name"] = bare
                        return
                    if not result["fund_name"]:
                        result["fund_name"] = bare
            # Keep scanning later rows for a proper fund-token cell; a generic
            # fallback is only used if nothing better is found.  Don't return
            # after the first row just because a generic candidate was stored.


def _looks_like_brand_line(low: str) -> bool:
    """Row is an AMC-brand header ("UTI MUTUAL FUND", "ADITYA BIRLA SUN LIFE MF")."""
    if "mutual fund" in low:
        return True
    if low.strip().endswith("mf") and len(low.split()) <= 4:
        return True
    return False


def _find_header_row(df: pd.DataFrame) -> int | None:
    """Find the row that contains column headers."""
    for i in range(min(30, len(df))):
        row_vals = [str(v).strip().lower() for v in df.iloc[i] if pd.notna(v)]
        row_text = " ".join(row_vals)

        matches = 0
        for col_names in SEBI_COLUMNS.values():
            for name in col_names:
                if name in row_text:
                    matches += 1
                    break

        if matches >= 3:
            return i

    return 0


def _map_columns(headers: list[str]) -> dict[str, list[int]]:
    """Map SEBI standard column names to actual column indices."""
    col_map = {k: [] for k in SEBI_COLUMNS}

    for i, header in enumerate(headers):
        if not header:
            continue

        # "Rating / Industry" (or "Rating/Industry^") is the industry column for
        # equity and the credit-rating column for debt; never treat it as both.
        if "rating" in header and "industry" in header:
            col_map["sector"].append(i)
            continue

        for standard_name, aliases in SEBI_COLUMNS.items():
            for alias in aliases:
                if alias in header or header in alias:
                    col_map[standard_name].append(i)
                    break

    return col_map


def save_excel_parsed_data(data: dict, output_dir: Path, filename_stem: str) -> None:
    """Save parsed Excel data as JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / f"{filename_stem}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info(f"Saved JSON: {json_path}")

    all_holdings = []
    for scheme_name, scheme_data in data.get("schemes", {}).items():
        for holding in scheme_data.get("holdings", []):
            holding["scheme"] = scheme_name
            all_holdings.append(holding)

    if all_holdings:
        df = pd.DataFrame(all_holdings)
        csv_path = output_dir / f"{filename_stem}.csv"
        df.to_csv(csv_path, index=False)
        logger.info(f"Saved CSV: {csv_path} ({len(all_holdings)} holdings)")
