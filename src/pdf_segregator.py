"""Grouped-factsheet PDF segregation.

Large AMCs (Nippon India, Axis, Mirae, HSBC, ...) publish a single "grouped"
factsheet PDF containing one page (or small page-group) per scheme.  The
generic PDF parser treats the whole file as one document, so scheme counts come
out badly under-reported and ETFs/index funds are missed.

This module detects such grouped factsheets, splits the pages into per-scheme
segments and returns a parsed result whose ``schemes`` dict is keyed by scheme
name - the same shape the Excel parser produces - so downstream consumers
(report, all_schemes.csv) count every scheme correctly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)

# First line of a scheme page must not look like a section/annex header.
_SECTION_START = re.compile(
    r"^(EQUITY|DEBT|INDEX|ETF|FIXED|HYBRID|PERFORMANCE|KEY HIGHLIGHTS|"
    r"SYSTEMATIC|SCHEME PERFORMANCE|FUND MANAGER|FUNDS AT A GLANCE|"
    r"DISCLAIMERS|Annexure|How To Read|MARKET UPDATE|Macro|Gold & Silver|"
    r"Fixed Income|Tax Reckoner|I N D E X|MP|Minimum Investment|NAV|"
    r"Expense Ratio|ANNEXURE|Past performance|M P|Product Label|PRC Matrix|"
    r"FUND FACTS|Fund Facts|IDCW HISTORY|ASSET ALLOCATION|RISKOMETER|"
    r"GLOSSARY|INDEX|Important Sections|Registration Number|MF/0)",
    re.IGNORECASE,
)

# Markers that identify a page carrying a portfolio / factsheet for ONE scheme.
_PORTFOLIO_MARKER = re.compile(
    r"portfolio as on\s+[A-Za-z]+\s+\d{1,2},?\s*\d{4}",
    re.IGNORECASE,
)
_DETAILS_MARKER = re.compile(
    r"details as on\s+[A-Za-z]+\s+\d{1,2},?\s*\d{4}",
    re.IGNORECASE,
)
_MONTHLY_FACTSHEET = re.compile(
    r"(monthly factsheet|facts\s*heet|fact sheet)\s*[-:]?\s*(?:as on\s+)?"
    r"(?:\d{1,2}\s+[A-Za-z]+,?\s*\d{4}|[A-Za-z]+\s+\d{1,2},?\s*\d{4}|"
    r"[A-Za-z]+\s*\d{4})",
    re.IGNORECASE,
)

# Common corporate suffixes used to pull "Company Name 1.23%" holding rows out
# of the two-column factsheet layout (sectors/ratings get filtered out).
_SUFFIX = (
    r"(?:Limited|Ltd\.?|Corporation|Corp\.?|Industries|Finance|Financial|Bank|"
    r"Insurance|Technologies|Technology|Enterprises|Motors|Auto|Electronics|"
    r"Systems|Securities|Holdings|Group|Energy|Steel|Cement|Power|"
    r"Infrastructure|Communications|Chemicals|Healthcare|Pharmaceuticals|"
    r"Retail|Services|Metals|Beverages|Foods|Tobacco|Oil|Gas|Telecom|"
    r"Utilities|Realty|Global|International|Ventures|Capital|Warehousing|"
    r"Petroleum|Investments|Fertilizers|Constructions|Composites|Software|"
    r"Solutions|Network|Media|Chemicals|Motors)"
)
_HOLDING_RE = re.compile(
    r"((?:[A-Z][A-Za-z0-9&\.,'()\-]+ ){1,7}" + _SUFFIX + r")\s+"
    r"(\d{1,3}(?:[.,]\d{1,3})?)(?:\s*%|$)"
)

# Sector / fund-detail words that bleed into the company column in the
# two-column layout; stripped from the start of extracted holding names.
_SECTOR_WORDS = re.compile(
    r"^(?:Details as on\s+\d{1,2}\s+\w+\s+\d{4}\s+|Type of Scheme\s+|"
    r"Aerospace & Defense|Automobiles|Auto Components|Banks|Capital Markets|"
    r"Cement & Cement Products|Construction|Construction Materials|"
    r"Consumer Durables|Diversified FMCG|Electrical Equipment|Energy|Finance|"
    r"Ferrous Metals|Financial Technology|Food Products|Food & Beverages|"
    r"Healthcare Services|Housing Finance|Industrial Manufacturing|"
    r"Insurance|IT - Software|IT Services|Leisure Services|Media & Entertainment|"
    r"Metals & Mining|Non - Ferrous Metals|Petroleum Products|Power|"
    r"Pharmaceuticals & Biotechnology|Retailing|Telecom - Services|"
    r"Transport Infrastructure|Transport Services|Utilities|Chemicals|"
    r"Agricultural Food|Textiles|Forest Materials|Fertilizers|Paper|"
    r"Gas|Liquefied Gas|Communication|Realty|Commodities|Logistics|"
    r"Scheme\s+|Equity\s+|Debt\s+|Cash & Other|Net Current|Treasury|"
    r"Fund\s+|Investors\s+|Benchmark\s+|NAV\s+)*",
    re.IGNORECASE,
)

# After sector stripping the remaining token may still start with a stray word
# (e.g. "Products Britannia Industries Limited") - drop leading words that are
# clearly not part of the company name and not a known corporate-suffix lead.
_DROP_PREFIX = re.compile(
    r"^(?:Products|Defense|Auto|Engineering|Mining|Construction|Vehicles|"
    r"Communications|Technologies|Solutions|Services|Industries|Holdings|"
    r"Enterprise|Enterprises|Finance|Financial|Pharma|Healthcare|Software|"
    r"Systems|Network|Power|Energy|Metals|Consumer|Retail|Foods|Beverages|"
    r"Chemicals|Petroleum|Steel|Cement|Infrastructure|Logistics|Telecom|"
    r"Media|Oil|Gas|Realty|Utilities|Commodities|Electrical|Machinery|"
    r"Engineering|Trading|International|Global|Capital|Ventures|Group|"
    r"India)\s+",
    re.IGNORECASE,
)

# Fund-manager names bleed into the company column in Mirae's top-10 layout
# ("Mr. Gaurav Misra HDFC Bank Ltd."). Strip "<Mr|Ms|Mrs>. <Name> " prefixes.
_FM_PREFIX = re.compile(
    r"^(?:Mr|Ms|Mrs|Dr)\.?\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+",
    re.IGNORECASE,
)

# Section-end markers - cut the holdings block here so NAV tables, SIP
# performance tables etc. don't leak into the holdings list.
_SECTION_END = (
    "industry allocation", "idcw history", "sip - if", "for scheme performance",
    "tracking error", "base expense", "load structure", "amfi classification",
    "allocation - top 10 sectors", "scheme riskometer", "product label",
    "prc matrix", "riskometer", "this product is suitable",
    "past performance may",
)

_DATE_RE = re.compile(
    r"(?:as on|as of|details as on|portfolio as on|factsheet as on)\s+"
    r"(\d{1,2}\s+[A-Za-z]+,?\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


def _is_scheme_page(text: str) -> bool:
    """True if *text* is the first page of a single-scheme factsheet."""
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    low = text.lower()
    # Axis "MP" (market pulse) block pages: scheme name buried under MP banner.
    if lines[0].upper() in ("MP", "M P") and "factsheet" in low:
        return True
    first = lines[0]
    if not first or len(first) < 3:
        return False
    if _SECTION_START.match(first):
        return False
    # Scheme name + FACTSHEET on the same first line (Axis/PPFAS pages).
    first_low = first.lower()
    if ("factsheet" in first_low or "fact sheet" in first_low) and (
        "portfolio" in low or "disclosure" in low or "fact sheet" in first_low
    ):
        return True
    # Mirae banner: line 0 == "MIRAE ASSET", scheme name on line 1.
    if first.strip().upper() == "MIRAE ASSET" and "monthly factsheet" in low:
        return True
    head = " ".join(lines[1:5]).lower()
    if _PORTFOLIO_MARKER.search(head) or _DETAILS_MARKER.search(head):
        return True
    if _MONTHLY_FACTSHEET.search(head):
        return True
    # Axis-style: scheme name on line 0, then "FACTSHEET", page carries PORTFOLIO.
    if "factsheet" in head and "portfolio" in low:
        return True
    return False


def _scheme_name(first_line: str, amc_hint: str = "") -> str:
    """Clean a scheme page's first line into a scheme name."""
    name = re.sub(r"\s+", " ", first_line).strip(" .")
    # Axis index-fund pages: "AXIS...INDEX FUND FACTSHEET" - drop the word.
    name = re.sub(r"\s*(FACTSHEET|FACT\s*SHEET)\s*[-:–]?\s*$", "", name, flags=re.IGNORECASE).strip()
    # PPFAS-style: "Parag Parikh Flexi Cap Fund FACT SHEET - JULY 2026"
    name = re.sub(
        r"\s+(FACT\s*SHEET|FACTSHEET)\s*[-:–]?\s*"
        r"(?:\d{1,2}\s+)?[A-Za-z]+\s*,?\s*\d{4}\s*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    # A bare "FACT SHEET - JULY 2026" header (no fund name) is a continuation
    # marker in PPFAS-style layouts - treat as empty so the caller skips it.
    if re.match(r"^(FACT\s*SHEET|FACTSHEET)\s*[-:–]?\s*", name, re.IGNORECASE):
        return ""
    if name.lower().startswith("mirae asset"):
        name = name[len("mirae asset"):].strip()
        if name:
            name = f"Mirae Asset {name}"
    return name


def _extract_date(text: str) -> str:
    m = _DATE_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _holdings_block(text: str) -> str:
    """Return the portfolio-holdings portion of a scheme page."""
    low = text.lower()
    start = len(text)
    for marker in ("portfolio top 10 holdings", "portfolio holdings",
                   "portfolio top holdings", "top 10 holdings",
                   "portfolio as on", "portfolio"):
        idx = low.find(marker)
        if idx != -1:
            start = min(start, idx)
    if start == len(text):
        return ""
    # Begin after the marker's line so an inline section-end on the same line
    # (e.g. Mirae "Portfolio Top 10 Holdings Allocation - Top 10 Sectors^")
    # doesn't cut the block to zero.
    seg = text[start:]
    newline = seg.find("\n")
    if newline != -1:
        seg = seg[newline + 1:]
    low_seg = seg.lower()
    end = len(seg)
    for marker in _SECTION_END:
        idx = low_seg.find(marker)
        if idx != -1:
            end = min(end, idx)
    return seg[:end]


def _extract_holdings(text: str) -> list[dict]:
    """Extract ``{company, percent_nav, isin}`` rows from a scheme page."""
    seg = _holdings_block(text)
    rows: list[dict] = []
    seen = set()
    for ln in seg.splitlines():
        for m in _HOLDING_RE.finditer(ln):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .*")
            name = _SECTOR_WORDS.sub("", name).strip(" .")
            name = _FM_PREFIX.sub("", name).strip(" .")
            name = _DROP_PREFIX.sub("", name).strip(" .")
            if len(name) < 3:
                continue
            pct = m.group(2)
            key = (name.lower(), pct)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "company": name,
                "percent_nav": pct,
                "isin": "",
            })
    return rows


def _first_scheme_line(lines: list[str]) -> str:
    """Scheme name from the first lines of a scheme page.

    Skips an Axis "MP" banner block (lines "MP" / "M P" / "G") and a Mirae
    "MIRAE ASSET" banner so the actual scheme name is used.
    """
    for i, ln in enumerate(lines[:8]):
        s = ln.strip()
        if s.upper() in ("MP", "M P", "G", "MIRAE ASSET"):
            continue
        return s
    return lines[0].strip() if lines else ""


def _build_schemes(page_texts: list[str]) -> dict[str, dict]:
    """Group pages into schemes and return a ``schemes`` dict."""
    schemes: dict[str, dict] = {}
    current: dict | None = None
    current_name = ""

    for text in page_texts:
        if _is_scheme_page(text):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            name = _scheme_name(_first_scheme_line(lines))
            if not name:
                # Generic "FACT SHEET - <date>" continuation header - merge into
                # the current scheme instead of starting a new (nameless) one.
                if current is not None:
                    current["holdings"].extend(_extract_holdings(text))
                continue
            if name == current_name:
                # Continuation page carrying the same scheme header again
                # (multi-page factsheets like PPFAS) - keep merging.
                current["holdings"].extend(_extract_holdings(text))
                if not current["date"]:
                    current["date"] = _extract_date(text)
                continue
            if current is not None and current_name:
                schemes[current_name] = current
            current = {
                "scheme_name": name,
                "fund_name": name,
                "date": _extract_date(text),
                "holdings": _extract_holdings(text),
                "sectors": [],
            }
            current_name = name
        elif current is not None:
            # Continuation page for the current scheme.
            current["holdings"].extend(_extract_holdings(text))
            if not current["date"]:
                current["date"] = _extract_date(text)

    if current is not None:
        schemes[current_name] = current

    return schemes


def parse_grouped_pdf(pdf_path: Path) -> dict | None:
    """Detect and parse a grouped factsheet PDF into per-scheme records.

    Returns a ``parse_pdf``-shaped dict (with a populated ``schemes`` key), or
    ``None`` when the PDF does not look like a grouped factsheet (fewer than
    two scheme pages), so the caller can fall back to the generic parser.
    """
    pdf_path = Path(pdf_path)
    page_texts: list[str] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_texts.append(page.extract_text() or "")
    except Exception as e:
        logger.warning(f"Grouped-pdf scan failed on {pdf_path.name}: {e}")
        return None

    scheme_pages = sum(1 for t in page_texts if _is_scheme_page(t))
    if scheme_pages < 2:
        return None

    schemes = _build_schemes(page_texts)
    if len(schemes) < 2:
        return None

    logger.info(
        f"Grouped factsheet {pdf_path.name}: segmented {len(schemes)} schemes "
        f"across {len(page_texts)} pages"
    )

    return {
        "source_file": str(pdf_path),
        "file_type": "pdf",
        "metadata": {
            "grouped_factsheet": True,
            "total_schemes": len(schemes),
            "date": next((s["date"] for s in schemes.values() if s["date"]), ""),
        },
        "schemes": schemes,
        "equity_holdings": [],
        "debt_holdings": [],
        "sector_allocation": [],
        "top_holdings": [],
        "cash_allocation": None,
        "raw_text": "\n\n".join(page_texts),
        "raw_tables": [],
    }
