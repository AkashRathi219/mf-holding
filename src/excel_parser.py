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
    "coupon": ["coupon (%)", "coupon %", "coupon"],
    "quantity": ["quantity", "qty", "no of shares", "face value"],
    "market_value": ["market value", "market/fair value", "mkt value", "fair value", "market value (rs"],
    "percent_nav": ["% to nav", "% to aum", "% of nav", "% nav", "percentage", "% to net assets", "weightage"],
    "derivative_pct_nav": ["derivative % to nav"],
    "unhedged_pct_nav": ["unhedged % to nav"],
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
    # HDFC-style layout labels (often placed in the ISIN column)
    "equity & equity related", "(a) listed / awaiting listing",
    "listed / awaiting listing on stock exchanges",
    "units issued by invits", "units issued by mutual funds",
    "money market instruments", "others", "unclassified",
}

_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _norm_ws(value: str) -> str:
    """Collapse newlines/repeated whitespace inside a cell ("Derivative\\n% to NAV")."""
    return re.sub(r"\s+", " ", str(value)).strip()


def _num(value) -> float | None:
    """Parse a numeric cell (handles lakhs commas, ₹ and (paren) negatives)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("₹", "").strip()
    if not s or s.lower() in ("na", "n/a", "-", ""):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.rstrip("%")
    try:
        out = float(s)
    except ValueError:
        return None
    return -out if neg else out


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

        deriv_sheets: dict[str, dict] = {}
        for sheet_name in xl.sheet_names:
            if sheet_name == index_sheet:
                continue

            df = pd.read_excel(excel_path, sheet_name=sheet_name, header=None)
            if df.empty or len(df) < 2:
                continue

            if _is_derivative_sheet(sheet_name, df):
                try:
                    deriv_sheets[sheet_name] = _parse_derivative_sheet(sheet_name, df)
                except Exception as e:  # never let a disclosure quirk kill parsing
                    logger.warning("Derivative sheet %r failed to parse: %s", sheet_name, e)
                continue

            parsed_sheet = _parse_sheet(df, sheet_name)
            if parsed_sheet and parsed_sheet.get("holdings"):
                result["schemes"][sheet_name] = parsed_sheet

        _bind_derivatives(result, deriv_sheets)

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
    isin_cols = col_map.get("isin", [])
    isin_idx = isin_cols[0] if isin_cols else None

    for idx in range(data_start, len(df)):
        row = df.iloc[idx]
        row_vals = [_norm_ws(v) if pd.notna(v) else "" for v in row]

        if all(v == "" for v in row_vals):
            continue

        non_empty_count = sum(1 for v in row_vals if v)
        company_val = row_vals[company_idx] if company_idx is not None and company_idx < len(row_vals) else ""

        # Section/sub-section headers ("EQUITY & EQUITY RELATED", "(a) Listed /
        # awaiting listing...", "DEBT INSTRUMENTS") occupy only a few cells and
        # are never holdings.  HDFC-style layouts place them in the ISIN column;
        # most other AMCs use the company column — scan both candidates.
        isin_cell = row_vals[isin_idx] if isin_idx is not None and isin_idx < len(row_vals) else ""

        # "Grand Total" closes the sheet — capture NAV denominator for
        # derivative-percentage math (mv col + ≈100% weight).
        if non_empty_count <= 3:
            for cand in (company_val, isin_cell):
                if cand and "grand total" in cand.lower():
                    mv_col = col_map.get("market_value", [])
                    pct_col = col_map.get("percent_nav", [])
                    nav_mv = _num(row_vals[mv_col[0]]) if mv_col and mv_col[0] < len(row_vals) else None
                    if nav_mv:
                        result["nav_lacs"] = nav_mv
                        result["nav_pct"] = (
                            _num(row_vals[pct_col[0]]) if pct_col and pct_col[0] < len(row_vals) else None)
                    break

        label_val = ""
        for cand in (company_val, isin_cell):
            if cand and non_empty_count <= 3 and _is_section_label(cand):
                label_val = cand
                break

        if label_val:
            low = label_val.lower()
            if "total" not in low:
                current_section = label_val.strip()
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


_SECTION_PREFIXES = (
    "listed / awaiting", "unlisted", "units issued",
    "debt instrument", "government securit", "state government", "central government",
    "non-convertible", "non convertible", "securitized debt", "securitised debt",
    "certificate of deposit", "commercial paper", "treasury bill",
    "state development loan", "pass through certificate", "arbitrage",
    "cash & cash equivalent", "net current asset", "short term deposit",
    "money market instrument", "reverse repo", "tri-party repo", "treps",
)


def _is_section_label(val: str) -> bool:
    """Heuristic: sparse-row cell that is a portfolio-section heading.

    Real instrument names are excluded by ISIN/digit checks; accepted labels are
    whitelisted SECTION_KEYWORDS (after stripping "(a) "-style enumerators),
    known heading prefixes ("Government Securities (Central/State)" etc.),
    ALL-CAPS headings, or listed/unlisted subsection lines.
    """
    v = val.strip()
    if not (3 < len(v) < 90):
        return False
    if _ISIN_PATTERN.match(v.upper()):
        return False
    if _num(v) is not None:
        return False
    low = re.sub(r"\s+", " ", v.lower())
    stripped = re.sub(r"^\(?[a-e]\)?[).:]?\s*", "", low).strip(". ")
    if low in SECTION_KEYWORDS or stripped in SECTION_KEYWORDS:
        return True
    if any(low.startswith(p) or stripped.startswith(p) for p in _SECTION_PREFIXES):
        return True
    if v.isupper() and len(v.split()) <= 12 and not any(c.isdigit() for c in v):
        return True
    return False


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
    """Map SEBI standard column names to actual column indices.

    Two-pass matching: exact alias matches claim a header first, then substring
    matches fill remaining headers.  This keeps specific sibling headers like
    "Derivative % to NAV" / "Unhedged % to NAV" from being swallowed by the
    generic "% to NAV" alias family (and vice versa).
    """
    col_map: dict[str, list[int]] = {k: [] for k in SEBI_COLUMNS}
    claimed: dict[int, str] = {}

    def norm(h: str) -> str:
        return re.sub(r"\s+", " ", str(h)).strip().lower()

    # Pass 1 — exact aliases win outright.
    for i, header in enumerate(headers):
        if not header:
            continue
        h = norm(header)
        for standard_name, aliases in SEBI_COLUMNS.items():
            if any(a == h for a in aliases):
                col_map[standard_name].append(i)
                claimed[i] = standard_name
                break

    # Pass 2 — substring matching for whatever is still unclaimed.
    for i, header in enumerate(headers):
        if not header or i in claimed:
            continue
        h = norm(header)

        # "Rating / Industry" (or "Rating/Industry^") is the industry column for
        # equity and the credit-rating column for debt; never treat it as both.
        if "rating" in h and "industry" in h:
            col_map["sector"].append(i)
            continue

        best = None
        best_len = 0
        for standard_name, aliases in SEBI_COLUMNS.items():
            for alias in aliases:
                if alias in h or h in alias:
                    if len(alias) > best_len:
                        best = standard_name
                        best_len = len(alias)
                    break
        if best:
            col_map[best].append(i)
            claimed[i] = best

    return col_map


# ---------------------------------------------------------------------------
# Derivative-disclosure sheets (SEBI circular CIR/IMD/DF/11/2010 format).
#
# Layouts vary across AMCs, so this is an adaptive block scanner rather than a
# fixed template parser:  section titles open typed blocks ("hedging positions
# through futures/options", "swaps", "credit derivatives"); each block infers
# its own column mapping from a synonym pool; rows are accepted on content (a
# textual underlying + at least one numeric), never on position.  Footer totals
# are captured when present ("reported") and always re-computed from the parsed
# positions ("computed"), so absent/malformed total rows degrade gracefully.
# ---------------------------------------------------------------------------

_DERIV_SHEET_TITLE_RE = re.compile(
    r"derivative\s+disclosure|disclosure\s+regarding\s+derivatives?", re.I)

_DERIV_OPENER_CTX_RE = re.compile(
    r"hedg\w+\s+position|other\s+than\s+hedg|positions?\s+through|"
    r"transactions\s+in\s+credit", re.I)

_BLOCK_KIND_RES = (
    ("credit_derivatives",
     re.compile(r"credit\s+(?:default\s+)?(?:swap|derivatives?)", re.I)),
    ("swaps", re.compile(r"\bswaps?\b", re.I)),
    ("options", re.compile(r"\boptions?\b", re.I)),
    ("futures", re.compile(r"\bfutures?\b", re.I)),
)

_PCT_TOTAL_MARKER_RE = re.compile(r"total\s*(?:%age|percentage)\b|%\s*of\s*existing\s*assets", re.I)
_SUMMARY_HINT_RE = re.compile(r"contract|notional\b|number\s+of|profit|\d\.\s|gross\s+notional", re.I)

# derivative-row fields that must contain numbers
_DERIV_NUMERIC_FIELDS = ("combined_qty", "long_pos", "short_pos", "notional",
                         "price_buy", "price_current", "margin", "market_value")


def _deriv_header_field(h_low: str) -> tuple[str, bool] | None:
    """Map a normalized derivative-table header cell to a canonical field.

    Returns (field, is_primary) or None. `is_primary` marks the field that can
    satisfy the block's minimum parsing requirement (an underlying name).
    """
    h = h_low.strip()
    if not h:
        return None
    if "scheme name" in h or h == "scheme":
        return ("scheme_tag", False)
    if "swap type" in h:
        return ("swap_type", False)
    if "reference entity" in h:
        return ("underlying", True)
    if any(k in h for k in ("underlying", "security", "instrument", "scrip")):
        return ("underlying", True)
    if "industry" in h or h == "sector":
        return ("industry", False)
    if "maturity" in h:
        return ("maturity", False)
    if "margin" in h:
        return ("margin", False)
    if "market value" in h or h == "mv":
        return ("market_value", False)
    if "notional" in h:
        return ("notional", False)
    if "price when purchased" in h or ("purchas" in h and "price" in h):
        return ("price_buy", False)
    if "current price" in h:
        return ("price_current", False)
    if "price" in h and "futures" in h:
        return ("price_buy", False)
    if "strike" in h or "premium" in h or "exercise" in h:
        return ("option_strike_or_premium", False)
    if "option type" in h or "call" in h or "put" in h:
        return ("option_type", False)
    if "long" in h and "short" in h:
        return ("combined_qty", False)
    if "long" in h:
        return ("long_pos", False)
    if "short" in h:
        return ("short_pos", False)
    return None


def _infer_deriv_columns(row_vals: list[str]) -> dict[str, int]:
    """Infer a column map {field: idx} from one candidate header row."""
    cols: dict[str, int] = {}
    for i, v in enumerate(row_vals):
        if not v:
            continue
        m = _deriv_header_field(v.lower())
        if m and m[0] not in cols:
            cols[m[0]] = i
    return cols


def _is_plausible_position_row(vals: list[str], cols: dict[str, int]) -> bool:
    und_idx = cols.get("underlying")
    if und_idx is None or und_idx >= len(vals):
        return False
    und = vals[und_idx]
    if sum(c.isalpha() for c in und) < 3:
        return False
    numeric_hits = 0
    for f in _DERIV_NUMERIC_FIELDS:
        idx = cols.get(f)
        if idx is not None and idx < len(vals) and _num(vals[idx]) is not None:
            numeric_hits += 1
    return numeric_hits >= 1


def _deriv_block_kind(text: str) -> tuple[str | None, bool | None]:
    """Classify a lone title cell as (kind, hedging) block opener."""
    low = _norm_ws(text).lower()
    if len(low) < 14 or not _DERIV_OPENER_CTX_RE.search(low):
        return None, None
    if "derivative disclosure" in low or "disclosure regarding" in low:
        return None, None
    for kind, rx in _BLOCK_KIND_RES:
        if rx.search(low):
            hedging = "other than hedg" not in low
            return kind, hedging
    return None, None


def _is_derivative_sheet(sheet_name: str, df: pd.DataFrame) -> bool:
    low_name = sheet_name.lower()
    if low_name.startswith("derivative"):
        return True
    probe_rows = min(len(df), 10)
    for i in range(probe_rows):
        for v in df.iloc[i]:
            if pd.notna(v) and _DERIV_SHEET_TITLE_RE.search(str(v)):
                return True
    return False


def _parse_derivative_sheet(sheet_name: str, df: pd.DataFrame) -> dict:
    """Adaptively parse a derivative-disclosure sheet.

    Output shape (AMC/scheme agnostic; totals robust to absence):

    {"parts": [{"kind", "hedging", "nil", "columns": [...], "positions": [...],
                "subtotal_rows": [...], "computed_totals": {...}, "source_sheet"}...],
     "summary_tables": [{"headers": [...], "values": [...]}...],
     "reported_pct_of_assets": float|None,
     "_content_names": [...], "_sheet_codes": set()}
    """
    out: dict = {
        "parts": [],
        "summary_tables": [],
        "reported_pct_of_assets": None,
        "_content_names": [],
        "_sheet_codes": set(),
    }
    content_names: list[str] = []
    sheet_codes: set = set()

    cur: dict | None = None

    def finalize(block: dict | None) -> None:
        nonlocal cur
        if block is None:
            return
        mvs = [p.get("market_value") for p in block["positions"]
               if isinstance(p.get("market_value"), (int, float))]
        notionals = [p.get("notional") for p in block["positions"]
                     if isinstance(p.get("notional"), (int, float))]
        qtys = [p.get("quantity") for p in block["positions"]
                if isinstance(p.get("quantity"), (int, float))]
        block["computed_totals"] = {
            "n_positions": len(block["positions"]),
            "market_value_lacs": round(sum(mvs), 6) if mvs else 0.0,
            "notional_lacs": round(sum(notionals), 6) if notionals else 0.0,
            "net_quantity": sum(qtys) if qtys else 0,
        }
        out["parts"].append(block)
        cur = None

    pct_pending_scan = 0

    for idx in range(len(df)):
        row = df.iloc[idx]
        raw_vals = [_norm_ws(v) if pd.notna(v) else "" for v in row]
        texts = [v for v in raw_vals if v]
        if not texts:
            continue
        joined_low = " ".join(texts).lower()

        # -- reported "% of assets" footer -----------------------------------
        if pct_pending_scan > 0:
            for v in texts:
                n = _num(v)
                if n is not None and -1e6 < n < 1e6 and not re.fullmatch(
                        r"[a-z .%/()\-]*", v.lower()):
                    out["reported_pct_of_assets"] = n
                    pct_pending_scan = 0
                    break
            else:
                pct_pending_scan -= 1

        first = texts[0]
        if _PCT_TOTAL_MARKER_RE.search(joined_low):
            pct_pending_scan = 4
            continue

        # -- summary tables (contract counts / notional footers) -------------
        hint_cells = sum(1 for t in texts if _SUMMARY_HINT_RE.search(t))
        if hint_cells >= 2 and len(texts) >= 3:
            # capture the nearest following row carrying numbers (≤2 below)
            for j in range(idx + 1, min(idx + 3, len(df))):
                nxt = [_norm_ws(v) if pd.notna(v) else "" for v in df.iloc[j]]
                nxt_texts = [v for v in nxt if v]
                if not nxt_texts:
                    continue
                if any(_num(v) is not None for v in nxt_texts):
                    out["summary_tables"].append({
                        "headers": texts,
                        "values": nxt_texts,
                    })
                    break
            continue

        # -- block opener? ----------------------------------------------------
        title_cell = first if len(texts) <= 2 else ""
        kind, hedging = _deriv_block_kind(title_cell) if title_cell else (None, None)
        if kind:
            finalize(cur)
            # ": Nil" markers mean an empty section — still recorded for fidelity.
            cur = {
                "kind": kind,
                "hedging": bool(hedging),
                "nil": joined_low.rstrip().endswith(": nil"),
                "title": _norm_ws(first),
                "columns_raw": [],
                "cols": {},
                "positions": [],
                "subtotal_rows": [],
                "source_sheet": sheet_name,
                "row_index": idx,
            }
            continue

        # -- inside an open block ---------------------------------------------
        if cur is not None:
            if not cur["cols"]:
                inferred = _infer_deriv_columns(raw_vals)
                needs_map = any(k in joined_low for k in
                                ("underlying", "instrument", "scrip", "security",
                                 "industry", "price", "notional", "swap type"))
                if "underlying" in inferred or (needs_map and inferred):
                    cur["cols"] = inferred
                    cur["columns_raw"] = texts
                    continue
            elif _is_plausible_position_row(raw_vals, cur["cols"]):
                cols = cur["cols"]

                def g(field: str) -> str:
                    i = cols.get(field)
                    return raw_vals[i] if i is not None and i < len(raw_vals) else ""

                pos: dict = {"row_index": idx + 1}

                # scheme tag / swap leg metadata
                tag = g("scheme_tag")
                if tag:
                    pos["scheme_tag"] = tag
                    if tag.isupper() and tag.isalnum() and len(tag) <= 10:
                        sheet_codes.add(tag)
                stype = g("swap_type")
                if stype:
                    pos["swap_type"] = stype

                und = g("underlying")
                pos["underlying"] = und
                if sum(c.isalpha() for c in und) >= 8 and (
                        "fund" in und.lower() or "mutual" in und.lower()):
                    content_names.append(und)
                ind = g("industry")
                if ind:
                    pos["industry"] = ind

                combined = _num(g("combined_qty"))
                qlong = _num(g("long_pos"))
                qshort = _num(g("short_pos"))
                if combined is not None:
                    pos["quantity"] = combined
                    pos["side"] = "short" if combined < 0 else ("long" if combined > 0 else None)
                elif qlong is not None or qshort is not None:
                    qty = (qlong or 0.0) - (qshort or 0.0)
                    pos["quantity"] = qty
                    pos["side"] = "short" if qty < 0 else ("long" if qty > 0 else None)
                else:
                    leg = " / ".join(t for f in ("long_pos", "short_pos") for t in [g(f)] if t)
                    if leg:
                        pos["long_short_text"] = leg
                    lt = g("option_type")
                    if lt:
                        pos["option_type"] = lt
                    strike = _num(g("option_strike_or_premium"))
                    if strike is not None:
                        pos["strike_or_premium"] = strike

                for f in ("notional", "price_current", "margin"):
                    n = _num(g(f))
                    if n is not None:
                        pos[f] = n
                pb = _num(g("price_buy"))
                if pb is not None:
                    pos["price_when_purchased"] = pb
                mv = _num(g("market_value"))
                if mv is not None:
                    pos["market_value"] = mv
                mat = g("maturity")
                if mat:
                    pos["maturity"] = mat

                cur["positions"].append(pos)
                continue

            # numeric-only subtotal line under the block
            nums = sum(1 for t in texts if _num(t) is not None)
            if nums >= 2 and not cur["cols"]:
                cur["subtotal_rows"].append(texts)
                continue
            if cur["cols"] and not _is_plausible_position_row(raw_vals, cur["cols"]) and nums >= 2:
                cur["subtotal_rows"].append(texts)
                continue
            # foreign content closes nothing implicitly — tolerated

    finalize(cur)

    # harvest plain-name candidates from unbound cells ("Scheme Name | %")
    for part in out["parts"]:
        for st in out["summary_tables"]:
            for v in st["values"]:
                if "fund" in v.lower() and len(v) > 12:
                    content_names.append(v)

    out["_content_names"] = sorted(set(content_names))
    out["_sheet_codes"] = sheet_codes
    return out


def _nm_key(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def _match_deriv_to_scheme(d_name: str, d: dict, schemes: dict) -> str | None:
    """Bind a parsed derivative sheet to its owning portfolio sheet."""
    dl = d_name.lower()
    base = re.sub(r"^derivativ(?:e|es)", "", dl).strip("_ ")

    # 1) sheet-code suffix: "DerivativeHDFCMY" ↔ main sheet "HDFCMY"
    for s in schemes:
        sl = s.lower()
        if base and (sl == base or dl.endswith(sl) or base.endswith(sl)):
            return s

    codes = d.get("_sheet_codes") or set()
    if codes:
        for s in schemes:
            if s.upper().strip() in codes or s.split()[0].upper() in codes:
                return s

    # 2) full fund names mentioned inside the derivative sheet
    content = d.get("_content_names") or []

    def fund_of(s: str) -> str:
        return ((schemes[s] or {}).get("fund_name") or s)

    nm = {_nm_key(fund_of(s)): s for s in schemes}
    for cn in content:
        k = _nm_key(cn)
        for ck, s in nm.items():
            if ck and len(ck) > 10 and (ck in k or k in ck):
                return s

    # 3) token overlap fallback against main-sheet names themselves
    dk = _nm_key(base)
    best_s, best_r = None, 0.0
    for s in schemes:
        sk = _nm_key(s)
        if not sk or not dk:
            continue
        shorter, longer = sorted((dk, sk), key=len)
        ratio = len(shorter) / len(longer)
        inter = sum(1 for ch in shorter if ch in longer) / max(len(shorter), 1)
        score = ratio * (0.5 + 0.5 * inter)
        if score > best_r:
            best_s, best_r = s, score
    if best_r >= 0.75:
        return best_s
    return None


def _bind_derivatives(result: dict, deriv_sheets: dict[str, dict]) -> None:
    """Attach parsed derivative disclosures to their portfolio schemes and
    populate scheme-level derivatives_pct_nav with both reported & computed."""
    schemes = result.setdefault("schemes", {})
    if not deriv_sheets:
        return
    bound_targets = set()
    for d_name, d in deriv_sheets.items():
        target = _match_deriv_to_scheme(d_name, d, schemes)
        if target is None:
            result.setdefault("derivatives_unbound", {})[d_name] = d
            logger.warning("Derivative sheet %r could not be bound to a scheme "
                           "(content=%s codes=%s)", d_name,
                           d.get("_content_names"), d.get("_sheet_codes"))
            continue
        bound_targets.add(target)
        holder = schemes[target].setdefault("derivatives", {"parts": [], "summary_tables": []})
        holder.setdefault("sources", []).append(d_name)
        for part in d.get("parts", []):
            part.setdefault("source_sheet", d_name)
            holder["parts"].append(part)
        holder.setdefault("summary_tables", []).extend(d.get("summary_tables", []))
        rep = d.get("reported_pct_of_assets")
        if rep is not None:
            prev = schemes[target].get("derivatives_reported_pct")
            schemes[target]["derivatives_reported_pct"] = rep if prev is None else prev + rep

    for target in bound_targets:
        sd = schemes[target]
        holder = sd.get("derivatives") or {}
        parts = holder.get("parts", [])
        mv_total = 0.0
        notional_total = 0.0
        n_pos = 0
        computed_by_kind: dict[str, float] = {}
        for p in parts:
            ct = p.get("computed_totals") or {}
            mv = ct.get("market_value_lacs") or 0.0
            mv_total += mv
            notional_total += ct.get("notional_lacs") or 0.0
            n_pos += ct.get("n_positions") or 0
            key = f"{p['kind']}:{'hedging' if p['hedging'] else 'other'}"
            computed_by_kind[key] = round(computed_by_kind.get(key, 0.0) + mv, 6)
        computed_pct = None
        nav_lacs = sd.get("nav_lacs")
        if nav_lacs and abs(nav_lacs) > 0:
            computed_pct = round(mv_total / nav_lacs * 100.0, 4)
        sd["derivatives_pct_nav"] = {
            "reported": sd.pop("derivatives_reported_pct", None),
            "computed": computed_pct,
        }
        sd["derivatives_summary"] = {
            "n_positions": n_pos,
            "mv_total_lacs": round(mv_total, 6),
            "notional_total_lacs": round(notional_total, 6),
            "by_kind_mv": computed_by_kind,
        }


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
