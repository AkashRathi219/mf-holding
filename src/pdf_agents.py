"""PDF parsing agent network.

Split large / grouped PDFs into small per-scheme (or per-page-chunk) sub-PDFs
with fast PyMuPDF, then dispatch worker agents - running in a process pool so
the GIL doesn't serialise pdfplumber - to read holdings from each section, and
finally merge the section results back into a single ``parse_pdf``-shaped dict.

Design
------
* ``PdfSplitterAgent`` - one fast PyMuPDF pass (per-page reading-order text) to
  detect scheme boundaries, then ``insert_pdf`` to write small section PDFs.
  Scheme-boundary detection is *dictionary-first*: the per-AMC fund-name list
  from AMFI NAVAll.txt (``src/amfi_nav.py``) is used to find which page starts
  a scheme's factsheet, because many grouped-factsheet layouts (ABSL
  ``Product Label`` headers, Motilal leading page numbers, Edelweiss/Franklin
  banner lines) defeat first-line-only regexes.
* worker function ``_parse_section_worker`` - opens ONE section sub-PDF with
  pdfplumber (cheap on a few pages) and returns its text / tables / scheme,
  including table-based holdings rows for the new layouts.
* ``PdfCoordinatorAgent`` - orchestrates split -> parallel parse -> merge, with
  a legacy fallback if the process pool cannot be created.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from src.amfi_nav import get_nav, norm
from src.pdf_segregator import (
    _extract_holdings,
    _extract_date,
    _is_scheme_page,
    _scheme_name,
    _first_scheme_line,
    _build_schemes,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Shared process pool - reused across parse() calls so worker spawn cost is
# paid once per process, not once per PDF.
_POOL = None
_POOL_WORKERS = 0
_POOL_LOCK = threading.Lock()


def _get_pool(max_workers: int) -> ProcessPoolExecutor | None:
    """Return a shared process pool (re)created when worker count changes."""
    global _POOL, _POOL_WORKERS
    with _POOL_LOCK:
        if _POOL is not None and _POOL_WORKERS == max_workers:
            return _POOL
        if _POOL is not None:
            try:
                _POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _POOL = None
        try:
            _POOL = ProcessPoolExecutor(
                max_workers=max_workers,
                initializer=_section_worker_preamble,
            )
            _POOL_WORKERS = max_workers
            return _POOL
        except (OSError, ImportError, RuntimeError) as e:
            logger.warning(f"Could not start process pool: {e}")
            return None


def _shutdown_pool() -> None:
    """Shut the shared pool down (call at end of a batch/CLI run)."""
    global _POOL, _POOL_WORKERS
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.shutdown(wait=False)
            except Exception:
                pass
            _POOL = None
        _POOL_WORKERS = 0


def _fitz_reading_text(page) -> str:
    """Reconstruct reading-order text from a fitz page.

    ``page.get_text()`` returns blocks in z-order which breaks the two-column
    factsheet layout (sector names leak before holdings); sorting word lines by
    (y, x) mirrors pdfplumber's reading order so the existing scheme-detection
    regexes keep working.
    """
    d = page.get_text("dict")
    lines = []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            y = min(sp["bbox"][1] for sp in spans)
            x = min(sp["bbox"][0] for sp in spans)
            txt = "".join(sp.get("text", "") for sp in spans)
            if txt.strip():
                lines.append((y, x, txt))
    lines.sort(key=lambda t: (round(t[0], 1), t[1]))
    out: list[str] = []
    last_y = None
    for y, x, txt in lines:
        if last_y is not None and abs(y - last_y) <= 3 and out:
            out[-1] = out[-1] + " " + txt
        else:
            out.append(txt)
        last_y = y
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Scheme-boundary detection helpers
# ---------------------------------------------------------------------------

_PAGE_NUM_RE = re.compile(r"^\d{1,4}$")

# Lines that may precede the real scheme header in a page layout and must be
# skipped so the scheme name surfaces to line 0.
_BANNER_EXACT = {
    "mp", "m p", "g", "mirae asset", "index", "i n d e x", "think wealth creation",
    "invest now", "r: risk probability", "risk probability", "www.franklintempletonindia.com",
    "aditya birla sun life mutual fund", "edelweiss financial services",
    "understanding the factsheet", "contents", "risk-o-meter",
    "investor awareness program",
}
_BANNER_PREFIX = (
    "www.", "@", "mutual fund investments are subject", "read all scheme related",
    "entity name:", "copyright ©", "copyright (c)",
)

# ABSL group-page marker: the page below is a holdings page of the current
# scheme, not a new scheme header.
_ABSL_HOLDINGS_MARKER = re.compile(
    r"^(sector/issuer name|rating proﬁle of portfolio|rating profile of portfolio)",
    re.IGNORECASE,
)

# Non-company labels that leak into the company-name column of holdings tables.
_SECTION_NAME = {
    "treasury bills", "treasury bills & sdl", "government bonds", "government securities",
    "government bond", "money market instruments", "net cash and cash equivalents",
    "net cash", "net current assets", "grand total", "total", "debt & debt related",
    "debt & debt related mo", "debt & debt related tr", "equity & equity related",
    "equity & equity related instruments", "derivatives", "equity", "debt",
    "cash & other assets", "cash and cash equivalents", "cb & sdl", "bank deposits",
    "repos & reverse repos", "triparty repos", "certificates of deposit",
    "commercial papers", "cps & cds", "bonds", "sdl", "reverse repos",
    "mutual fund units", "other debt instruments", "pass through certificates",
    "security receipts", "treasury bills & sdl", "oil index", "gsecs",
    "corporate bonds", "aa and above", "aaa", "aa", "a", "bbb", "below investment grade",
    "unrated", "issuer rating", "fixed income", "floating rate bonds",
    "corporate debt", "gilts", "other assets", "alternative investment fund units",
    "aa/aa- and equivalent", "aa/aa and equivalent", "month end aum", "annualised portfolio ytm",
    "modified duration", "residual maturity", "average maturity", "inception date",
    "exit load", "total debt holdings", "total gilts",
}
_SECTION_RE = re.compile(
    r"^(?:Treasury Bills|Government Bond|Money Market|Net Cash|Grand Total|"
    r"Debt & Debt Related|Equity & Equity Related|Derivatives|Total|"
    r"Cash & Other|Bank Deposits|Repos|Reverse Repos|Certificates of Deposit|"
    r"Commercial Papers|CBs|SDLs|Triparty|Mutual Fund Units|Corporate Bonds|"
    r"Corporate Debt|Gilts|Other Assets|Alternative Investment|"
    r"AA/AA-|AA/AA|AAA|AA and above|Below Investment Grade|Unrated|Fixed Income|"
    r"Other Debt|Pass Through|Security Receipts|Month End AUM|Annualised|"
    r"Modified Duration|Residual Maturity|Average Maturity|Inception Date|Exit Load)\b",
    re.IGNORECASE,
)
# Sector labels that appear BETWEEN the company name and the % in tabular-text
# layouts (Groww: "ICICI Bank Limited Banks 9.15%").  Longest-first so
# multi-word sectors strip correctly.
_SECTOR_LABELS = [
    "Non - Ferrous Metals", "Non-Ferrous Metals", "Pharmaceuticals & Biotechnology",
    "Telecom - Services", "Media & Entertainment", "Agricultural Food",
    "Consumer Durables", "Capital Markets", "Construction Materials",
    "Transport Infrastructure", "Transport Services", "Fertilizers & Agro Chemicals",
    "Healthcare Services", "Housing Finance", "Financial Technology",
    "Industrial Manufacturing", "Industrial Products", "Leisure Services",
    "Electrical Equipment", "IT - Software", "IT Services", "Food Products",
    "Cement & Cement Products", "Diversified FMCG", "Metals & Mining",
    "Petroleum Products", "Fertilizers & Agro", "Food & Beverages",
    "Ferrous Metals", "Insurance", "Automobiles", "Banking", "Banks", "Cement",
    "Chemicals", "Commodities", "Communication", "Construction", "Consumer",
    "Diversified", "Energy", "Fertilizers", "Finance", "FMCG", "Gas",
    "Healthcare", "Infrastructure", "Logistics", "Metals", "Oil", "Paper",
    "Pharma", "Power", "Realty", "Retailing", "Services", "Software",
    "Steel", "Telecom", "Textiles", "Utilities",
]
_SECTOR_LABELS.sort(key=len, reverse=True)
# Fund-statistics labels that must never become holding rows ("Portfolio
# Turnover 0.30", "Standard Deviation 16.16", ...).
_METRIC_RE = re.compile(
    r"^(?:Portfolio Turnover|Standard Deviation|Sharpe Ratio|Treynor Ratio|"
    r"Information Ratio|Sortino Ratio|Jensen|Alpha|Beta|Expense Ratio|"
    r"Yield to Maturity|Macaulay Duration|Modified Duration|Average Maturity|"
    r"Residual Maturity|Volatility|Benchmark|NAV|AUM|Mean Return|Median|"
    r"Minimum Investment|Inception|Fund Manager|No. of Stocks|Portfolio Size|"
    r"P/E|P/B|Dividend Yield|Asset Size)\b",
    re.IGNORECASE,
)
# Sector / rating allocation rows inside a holdings table (ABSL "Banks",
# "Automobiles", "IT - Software", ...) that must not become holding rows.
_SECTOR_RE = re.compile(
    r"^(?:Banks|Banking|Automobiles|Auto Components|Capital Markets|Cement|"
    r"Cement & Cement Products|Construction|Construction Materials|"
    r"Consumer Durables|Diversified FMCG|Electrical Equipment|Energy|Finance|"
    r"Ferrous Metals|Financial Technology|Food Products|Food & Beverages|"
    r"Healthcare Services|Healthcare|Housing Finance|Industrial Manufacturing|"
    r"Industrial Products|Insurance|IT - Software|IT Software|IT Services|"
    r"Leisure Services|Media & Entertainment|Metals & Mining|Metals|"
    r"Non - Ferrous Metals|Non-Ferrous Metals|Petroleum Products|Power|"
    r"Pharmaceuticals & Biotechnology|Pharma|Retailing|Realty|"
    r"Telecom - Services|Telecom|Transport Infrastructure|Transport Services|"
    r"Utilities|Chemicals|Agricultural Food|Textiles|Forest Materials|"
    r"Fertilizers|Fertilizers & Agro|Paper|Gas|Liquefied Gas|Communication|"
    r"Commodities|Logistics|Beverages|Consumer Services|Diversified|"
    r"FMCG|Fertilizers & Agro Chemicals)\b",
    re.IGNORECASE,
)


def _strip_front_matter(lines: list[str]) -> list[str]:
    """Remove leading page-number / banner lines so the scheme name is line 0."""
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        low = s.lower()
        if _PAGE_NUM_RE.match(s) and i < 3:
            i += 1
            continue
        if low in _BANNER_EXACT or low.startswith(_BANNER_PREFIX):
            i += 1
            continue
        break
    return lines[i:]


_MONTH_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}

# Compound scheme words AMFI writes without a space but factsheets spell apart
# ("Groww Largecap Fund" vs "GROWW LARGE CAP FUND", "Multicap" vs "MULTI CAP").
_COMPOUND_WORDS = {
    "largecap": "large cap", "midcap": "mid cap", "smallcap": "small cap",
    "flexicap": "flexi cap", "multicap": "multi cap",
    "largeandmidcap": "large and mid cap", "largemidcap": "large and mid cap",
    "smidcap": "smid cap", "largeandmid": "large and mid",
}


def _split_compounds(text: str) -> str:
    toks = text.split()
    return " ".join(_COMPOUND_WORDS.get(t, t) for t in toks)


def _match_key(fund: str) -> str:
    """Normalize a fund name for window matching.

    Strips parenthetical annotations ("(formerly known as ...)") and date
    tokens, expands AMFI compound spellings, and drops trailing generic tokens
    (Fund/Plan/Scheme), so the dict name can be matched contiguously against a
    factsheet page's own (cleaner) name.
    """
    s = re.sub(r"\([^)]*\)", " ", fund)
    key = _strip_dates(_split_compounds(norm(s)))
    toks = key.split()
    while toks and toks[-1] in ("fund", "plan", "scheme"):
        toks.pop()
    return " ".join(toks)


def _strip_dates(text: str) -> str:
    """Drop standalone month/date tokens (``june 2026``) from normalized text.

    Grouped factsheets interleave the factsheet month into the scheme name
    ("...Nifty Next 50 Index" / "June 2026 Fund"); removing the date tokens lets
    the fund name be matched contiguously.  Fund names that themselves contain a
    month-year (e.g. ``Nifty SDL Apr 2027 Index Fund``) are stripped the same
    way before comparison, so both sides stay consistent.
    """
    toks = text.split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in _MONTH_WORDS:
            if i + 1 < len(toks) and toks[i + 1].isdigit() and len(toks[i + 1]) == 4:
                i += 2  # skip "<month> <year>"
            else:
                i += 1  # skip a bare month name
            continue
        out.append(t)
        i += 1
    return " ".join(out)


@dataclass
class PdfSection:
    """One sub-PDF to be parsed by a worker agent."""

    start_page: int          # 0-based, inclusive
    end_page: int            # 0-based, exclusive
    scheme_name: str | None  # set when this section is one scheme
    sub_pdf: Path            # the split section file


@dataclass
class SplitResult:
    sections: list[PdfSection] = field(default_factory=list)
    page_texts: list[str] = field(default_factory=list)  # full doc, fitz text
    page_tables: list = field(default_factory=list)      # populated for flat docs


class PdfSplitterAgent:
    """Split a PDF into small per-scheme or per-chunk section PDFs."""

    def __init__(self, work_dir: Path, chunk_pages: int = 6,
                 amc_name: str | None = None):
        self.work_dir = work_dir
        self.chunk_pages = chunk_pages
        self.amc_name = amc_name
        nav = get_nav()
        self.amc_key = self._resolve_amc_key(nav) if nav else None
        self.funds: list[str] = []
        seen_keys: set[str] = set()
        for fund in (nav.fund_names(self.amc_key) if nav and self.amc_key else []):
            key = _match_key(fund)
            if key and key not in seen_keys:
                seen_keys.add(key)
                self.funds.append(fund)
        self.norm_funds: set[str] = nav.normalized(self.amc_key) if nav and self.amc_key else set()
        self.brand_words: set[str] = nav.brand_words(self.amc_key) if nav and self.amc_key else set()
        # Precomputed match keys (parentheticals stripped, dates removed,
        # compound spellings expanded) for dict-based page detection.
        self.fund_keys: dict[str, str] = {f: _match_key(f) for f in self.funds}

    def _resolve_amc_key(self, nav) -> str | None:
        """Map an on-disk AMC folder name to an AMFI NAVAll header key."""
        if not self.amc_name:
            return None
        cand = (self.amc_name or "").replace("_", " ").strip().lower()
        if cand in nav.norm_funds:
            return cand
        best, best_len = None, 0
        for key in nav.norm_funds:
            if key == cand:
                return key
            if key in cand or cand in key:
                if len(key) > best_len:
                    best, best_len = key, len(key)
        return best

    # -- detection ---------------------------------------------------------

    def _cleaned_lines(self, text: str) -> list[str]:
        return _strip_front_matter([ln.strip() for ln in text.splitlines()])

    def _dict_matches(self, page_norm: str) -> list[tuple[str, int]]:
        """[(fund_name, char_pos)] for every known fund appearing in a page."""
        found: list[tuple[str, int]] = []
        for fund in self.funds:
            nf = self.fund_keys[fund]
            if len(nf) < 4:
                continue
            # Skip names that are basically just the AMC brand (false positives).
            if nf.split() and set(nf.split()) <= self.brand_words:
                continue
            pos = page_norm.find(nf)
            if pos != -1:
                found.append((fund, pos))
        found.sort(key=lambda t: t[1])
        return found

    def _dict_scheme_start(self, text: str) -> str | None:
        """Return a scheme name when a known AMFI fund clearly heads this page.

        Grouped factsheets often wrap the scheme name across lines (ABSL puts
        ``June 2026`` between the fund name and the trailing ``Fund``), so a
        contiguous whole-page match is unreliable.  Instead a small window of
        the top lines is scored against every known fund using token
        subsequence matching; the winner must be near-perfect and clearly ahead
        of any runner-up.  Pages that name many funds (contents / snapshot
        pages) are rejected up front as ambiguous.
        """
        if not self.norm_funds:
            return None
        lines = self._cleaned_lines(text)
        if not lines:
            return None

        # TOC / snapshot guard: if several lines each *start* with a full fund
        # name this is a contents listing, not a scheme header.  A line like
        # "Aditya Birla Sun Life Retirement" is only a prefix of sibling funds
        # and does NOT count (it is a legitimate scheme-header start).
        lines_with_full_name = 0
        for li in range(min(8, len(lines))):
            ln = norm(lines[li])
            if len(ln) < 5:
                continue
            for fund in self.funds:
                nf = norm(fund)
                if len(nf) < 6:
                    continue
                if ln.startswith(nf):
                    lines_with_full_name += 1
                    break
        if lines_with_full_name >= 3:
            return None

        for li in range(min(6, len(lines))):
            window = _match_key(" ".join(lines[li:li + 3]))
            if len(window) < 6:
                continue
            matched = [
                fund for fund in self.funds
                if len(self.fund_keys[fund]) >= 6
                and self.fund_keys[fund] in window
            ]
            if len(matched) == 1:
                return matched[0]
        return None

    def _marker_scheme_start(self, lines: list[str]) -> str | None:
        """Layout-specific scheme header markers (ABSL 'Product Label' pages)."""
        if not lines:
            return None
        if lines[0].lower() == "product label":
            for ln in lines[1:8]:
                s = ln.strip()
                if s and len(s) >= 8 and not _PAGE_NUM_RE.match(s):
                    return s
        return None

    def _scheme_start(self, text: str) -> str | None:
        """Combined scheme-page detection: markers, dictionary, legacy regex."""
        lines = self._cleaned_lines(text)
        if not lines:
            return None
        # Snapshot / listing pages ("Equity Snapshot", "Hybrid Snapshot", ...)
        # are one-line-per-fund overviews, never scheme headers.
        if "snapshot" in lines[0].lower():
            return None

        # 1. Dictionary hit (most reliable across AMC layouts).
        name = self._dict_scheme_start(text)
        if name:
            return name

        # 2. ABSL-style 'Product Label' header page.
        name = self._marker_scheme_start(lines)
        if name:
            return name

        # 3. Legacy first-line heuristics (Nippon/Axis/Mirae/PPFAS).  The
        # extracted name must look like a fund name, otherwise a cover page
        # ("EMPOWER", "Monthly Factsheet ...") would become a bogus scheme.
        cleaned = "\n".join(lines)
        if _is_scheme_page(cleaned):
            name = _scheme_name(_first_scheme_line(lines)) or ""
            if self._plausible_scheme_name(name):
                return name
        return None

    @staticmethod
    def _plausible_scheme_name(name: str) -> bool:
        n = name.strip()
        if len(n) < 5:
            return False
        if len(n.split()) < 2:
            return False
        if n.lower() in ("monthly", "factsheet", "fact sheet", "contents", "index",
                         "empower", "snapshot", "overview"):
            return False
        return True

    def _single_scheme_name(self, page_texts: list[str]) -> str | None:
        """Name of a whole-document single-scheme PDF (or None if ambiguous)."""
        if not self.norm_funds:
            return None
        full = _match_key(" ".join(page_texts))
        if len(full) < 8:
            return None
        matches = self._dict_matches(full)
        if not matches:
            return None
        best, pos = matches[0]
        frac = pos / max(len(full), 1)
        competitors = [p for _, p in matches[1:] if p < len(full) * 0.7]
        if frac < 0.15 and not competitors:
            return best
        return None

    # -- range building ----------------------------------------------------

    def _toc_ranges(self, raw_texts: list[str]) -> list[tuple[str, int, int]] | None:
        """Scheme ranges derived from a table-of-contents page.

        Some factsheets (LIC MF) put the scheme name in a header band that the
        reading-order text layer loses, so dict detection can't see most scheme
        starts; but the front matter contains a TOC mapping every scheme to its
        page number.  ``raw_texts`` must be the pages' plain ``get_text()``
        output (the TOC layout is "N." / name-with-dots / page on separate
        lines, which the reading-order reconstruction merges away).
        Returns [(scheme_name, start_page, end_page)] or None.
        """
        entries: list[tuple[str, int]] = []
        for text in raw_texts:
            lines = [l.strip() for l in text.splitlines()]
            if not ("index" in " ".join(lines).lower() or "contents" in " ".join(lines).lower()):
                continue
            i = 0
            while i < len(lines):
                if re.match(r"^\d+\.$", lines[i]) and i + 2 < len(lines):
                    name = re.sub(r"\s*\.{2,}\s*$", "", lines[i + 1])
                    name = re.sub(r"[\s.]+$", "", name).strip()
                    if lines[i + 2].isdigit() and name:
                        entries.append((name, int(lines[i + 2])))
                        i += 3
                        continue
                i += 1
        scheme_entries = [
            (n, p) for n, p in entries
            if re.search(r"(Fund|ETF|FOF|Plan|Yojana|Scheme|Liquid|Gilt)\b", n, re.I)
        ]
        if len(scheme_entries) < 5:
            return None
        # Calibrate the internal page numbers to doc indices via the first
        # scheme's page label.
        p0 = scheme_entries[0][1]
        offset = None
        for di, text in enumerate(raw_texts + [""] * 10):
            labels = {l.strip() for l in text.splitlines() if l.strip().isdigit()}
            if str(p0) in labels:
                offset = di - p0
                break
        if offset is None:
            return None
        n = len(self.page_texts)
        ranges: list[tuple[str, int, int]] = []
        for idx, (name, pg) in enumerate(scheme_entries):
            start = max(0, min(n - 1, pg + offset))
            end = (scheme_entries[idx + 1][1] + offset) if idx + 1 < len(scheme_entries) else n
            end = max(start + 1, min(n, end))
            ranges.append((name, start, end))
        return ranges

    def _scheme_ranges(self, texts: list[str]) -> list[tuple[str, int, int]]:
        """Return [(scheme_name, start_page, end_page)] for grouped factsheets."""
        ranges: list[tuple[str, int, int]] = []
        current_name = ""
        current_start = 0

        def flush(end: int) -> None:
            nonlocal current_name, current_start
            if current_name:
                ranges.append((current_name, current_start, end))

        for i, text in enumerate(texts):
            name = self._scheme_start(text)
            if name:
                if name == current_name:
                    continue  # continuation page carrying the same header
                flush(i)
                current_name = name
                current_start = i
            # else: continuation of the current scheme (or front matter skipped)
        flush(len(texts))
        return ranges

    def split(self, pdf_path: Path) -> SplitResult:
        """Fast fitz pass: detect scheme pages, write section sub-PDFs."""
        doc = fitz.open(pdf_path)
        try:
            page_texts = [_fitz_reading_text(page) for page in doc]
            self.page_texts = page_texts
            ranges = self._scheme_ranges(page_texts)
            raw_texts = [doc[i].get_text() or "" for i in range(min(6, doc.page_count))]
            toc_ranges = self._toc_ranges(raw_texts)
            # A TOC with many scheme entries is authoritative when the dict
            # path could only name a fraction of them (LIC header-band layout).
            if toc_ranges and len(toc_ranges) >= len(ranges) + 5:
                ranges = toc_ranges
            single_name = self._single_scheme_name(page_texts)
            grouped = len(ranges) >= 2 or (len(ranges) == 1 and ranges[0][1] == 0)
            sections: list[PdfSection] = []

            if grouped:
                if len(ranges) >= 2:
                    items = ranges
                else:
                    items = [(single_name or ranges[0][0], 0, doc.page_count)]
                for idx, (name, start, end) in enumerate(items):
                    sub = fitz.open()
                    try:
                        sub.insert_pdf(doc, from_page=start, to_page=end - 1)
                        sp = self.work_dir / f"sec_{idx:03d}.pdf"
                        sub.save(sp)
                        sub.close()
                        sections.append(PdfSection(
                            start_page=start, end_page=end,
                            scheme_name=name, sub_pdf=sp,
                        ))
                    finally:
                        try:
                            sub.close()
                        except Exception:
                            pass
            else:
                # Flat document - chunk pages so table extraction parallelises.
                n = doc.page_count
                for start in range(0, n, self.chunk_pages):
                    end = min(start + self.chunk_pages, n)
                    sub = fitz.open()
                    try:
                        sub.insert_pdf(doc, from_page=start, to_page=end - 1)
                        sp = self.work_dir / f"chunk_{start}_{end}.pdf"
                        sub.save(sp)
                        sub.close()
                        sections.append(PdfSection(
                            start_page=start, end_page=end,
                            scheme_name=None, sub_pdf=sp,
                        ))
                    finally:
                        try:
                            sub.close()
                        except Exception:
                            pass

            return SplitResult(sections=sections, page_texts=page_texts)
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# Worker agent (runs in the process pool - must be module-level & picklable)
# ---------------------------------------------------------------------------

def _section_worker_preamble() -> None:
    """Make ``src`` importable inside spawned worker processes."""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    if os.environ.get("MF_PDF_WORKER") != "1":
        os.environ["MF_PDF_WORKER"] = "1"


def _tables_to_holdings(raw_tables: list[list[list]]) -> list[dict]:
    """Convert pdfplumber tables into ``{company, percent_nav, isin}`` rows.

    Handles the ``Sector/Issuer Name | Rating | % to Net Assets`` layout found
    in ABSL / Edelweiss grouped factsheets.  Section rows (Treasury Bills,
    Money Market, Grand Total, ...) are filtered out.
    """
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tbl in raw_tables:
        if not tbl or len(tbl) < 2:
            continue
        header = [(h or "").strip().lower() for h in tbl[0]]
        cands = [
            i for i, h in enumerate(header)
            if "%" in h and any(w in h for w in ("net", "aum", "asset", "total"))
        ]
        if not cands:
            cands = [i for i, h in enumerate(header) if "%" in h]
        if cands:
            # Several AMC tables carry an empty "% of Total AUM" before the
            # populated "% of Net AUM" - pick the column with the most body
            # cells filled (tie-break toward "net").
            best_i, best_n = None, -1
            for i in cands:
                n = sum(1 for r in tbl[1:] if i < len(r) and (r[i] or "").strip())
                if n > best_n or (n == best_n and "net" in header[i]):
                    best_i, best_n = i, n
            pct_idx = best_i
        else:
            pct_idx = None
        if pct_idx is None:
            # No header with "%" (Franklin extracts the header row separately) -
            # infer the percentage column as the last numeric column whose
            # values all sit in a sane 0-100 band with a small spread.
            numeric_cols = []
            for ci in range(len(header)):
                vals = []
                for r in tbl[1:]:
                    if ci < len(r) and r[ci]:
                        try:
                            v = float((r[ci] or "").replace(",", "").strip())
                            vals.append(v)
                        except ValueError:
                            pass
                if vals and all(0 <= v <= 100 for v in vals) and max(vals) - min(vals) < 90:
                    numeric_cols.append(ci)
            if not numeric_cols:
                continue
            pct_idx = numeric_cols[-1]
        if pct_idx is None:
            continue
        for r in tbl[1:]:
            if len(r) <= pct_idx:
                continue
            pct = (r[pct_idx] or "").strip().replace("%", "").strip()
            if not pct:
                continue
            try:
                float(pct.replace(",", ""))
            except ValueError:
                continue
            name = ""
            for c in r[:pct_idx]:
                c = (c or "").strip()
                if c:
                    name = c
                    break
            name = re.sub(r"^\s*[●•·▪•]+\s*", "", name)
            name = re.sub(r"\s+", " ", name).strip(" .")
            if len(name) < 4:
                continue
            low = name.lower()
            if low in _SECTION_NAME or _SECTION_RE.match(name) or _METRIC_RE.match(name):
                continue
            if _SECTOR_RE.match(name):
                continue
            key = (low, pct)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"company": name, "percent_nav": pct, "isin": ""})
    return rows


def _text_tabular_holdings(full_text: str) -> list[dict]:
    """Extract ``{company, percent_nav, isin}`` from tabular *text* lines.

    Some factsheets (Groww) render holdings as one text line per row with the
    sector between the company and the percentage ("ICICI Bank Limited Banks
    9.15%") instead of an extractable table - the sector label is stripped and
    the company name is what remains.
    """
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in full_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"(\d{1,3}(?:[.,]\d{1,3})?)\s*%$", line)
        if not m:
            continue
        pct = m.group(1)
        body = line[: m.start()].strip()
        body = re.sub(r"^\s*[●•·▪*]+\s*", "", body)
        sector = None
        for s in _SECTOR_LABELS:
            if body.lower().endswith(s.lower()):
                sector = s
                break
        if sector is None:
            continue
        company = body[: len(body) - len(sector)].strip(" .-")
        if len(company) < 4:
            continue
        low = company.lower()
        if low in _SECTION_NAME or _METRIC_RE.match(company) or _SECTOR_RE.match(company):
            continue
        key = (low, pct)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"company": company, "percent_nav": pct, "isin": ""})
    return rows


def _parse_section_worker(args: dict) -> dict:
    """Parse one section sub-PDF with pdfplumber.

    args: {"sub_pdf": str, "scheme_name": str|None}
    Returns a dict of raw text/tables (flat chunk) or a scheme record.
    """
    _section_worker_preamble()
    import pdfplumber

    sub_pdf = Path(args["sub_pdf"])
    scheme_name = args.get("scheme_name")

    try:
        with pdfplumber.open(sub_pdf) as pdf:
            texts: list[str] = []
            raw_tables: list[list[list]] = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
                for table in page.extract_tables():
                    if table and len(table) > 1:
                        raw_tables.append(table)
    except Exception as e:
        return {"error": str(e), "sub_pdf": str(sub_pdf)}

    full_text = "\n\n".join(texts)

    if scheme_name:
        schemes = _build_schemes(texts)
        record = schemes.get(scheme_name)
        if record is None:
            # The splitter's dictionary-based detection is authoritative for
            # the scheme name; the legacy _build_schemes result (if any) may
            # carry a bogus single-scheme name ("FUND" from rotated text).
            record = {
                "scheme_name": scheme_name,
                "fund_name": scheme_name,
                "date": _extract_date(full_text),
                "holdings": [],
                "sectors": [],
            }
        holdings = _extract_holdings(full_text)
        holdings += _tables_to_holdings(raw_tables)
        holdings += _text_tabular_holdings(full_text)
        record["holdings"] = holdings or record.get("holdings", [])
        return {"scheme": record}

    return {"text": full_text, "tables": raw_tables}


# ---------------------------------------------------------------------------
# Coordinator agent
# ---------------------------------------------------------------------------

def _infer_amc_name(pdf_path: Path) -> str | None:
    """Recover the AMC name from the ``data/raw/pdfs/{AMC}/{YYYY}/{MM}/`` path layout."""
    parts = Path(pdf_path).parts
    try:
        idx = parts.index("pdfs")
    except ValueError:
        return None
    if idx + 1 < len(parts):
        return parts[idx + 1]
    return None


class PdfCoordinatorAgent:
    """Split a PDF, parse sections in parallel, merge into one result dict."""

    def __init__(self, max_workers: int | None = None, chunk_pages: int = 6):
        self.max_workers = max_workers
        self.chunk_pages = chunk_pages
        self._lock = threading.Lock()

    def _default_workers(self) -> int:
        if self.max_workers:
            return self.max_workers
        return min(os.cpu_count() or 2, 8)

    def parse(self, pdf_path: Path, legacy_fallback) -> dict | None:
        """Run the agent network; return a parse_pdf-shaped dict or None.

        ``legacy_fallback`` is the existing single-process parse callable used
        when the split produced no sections or the process pool is unusable.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            return None

        # The temporary section PDFs must stay alive while workers read them,
        # so split + dispatch + merge all happen inside the same tempdir.
        try:
            with tempfile.TemporaryDirectory(prefix="mf_agents_") as td:
                work = Path(td)
                splitter = PdfSplitterAgent(
                    work,
                    chunk_pages=self.chunk_pages,
                    amc_name=_infer_amc_name(pdf_path),
                )
                split = splitter.split(pdf_path)

                # Vector-rendered / image-only PDFs have no text layer - the
                # splitter can't see scheme boundaries, so hand off to the
                # legacy parser (which applies OCR).
                if not any(t.strip() for t in split.page_texts):
                    logger.info(f"No text layer in {pdf_path.name}; using legacy (OCR)")
                    return legacy_fallback()

                if not split.sections:
                    logger.info(f"No sections for {pdf_path.name}; using legacy parser")
                    return legacy_fallback()

                # Grouped factsheet -> merge scheme records.
                grouped = split.sections[0].scheme_name is not None
                if grouped:
                    return self._run_grouped(pdf_path, split)
                return self._run_flat(pdf_path, split, legacy_fallback)
        except Exception as e:
            logger.warning(f"Agent-network parse failed for {pdf_path.name}: {e}")
            return legacy_fallback()

    def _dispatch(self, sections: list[PdfSection]) -> list[dict]:
        """Run worker agents in a process pool; fall back to sequential."""
        tasks = [
            {"sub_pdf": str(s.sub_pdf), "scheme_name": s.scheme_name}
            for s in sections
        ]
        # Small jobs parse inline (avoids pool round-trip overhead).
        if len(tasks) <= 4:
            return [_parse_section_worker(t) for t in tasks]

        workers = self._default_workers()
        pool = _get_pool(workers)
        if pool is None:
            logger.warning("Process pool unavailable; parsing sequentially")
            return [_parse_section_worker(t) for t in tasks]

        # Keep result order aligned with sections via the index.
        try:
            future_map = {
                pool.submit(_parse_section_worker, t): i
                for i, t in enumerate(tasks)
            }
        except (OSError, RuntimeError) as e:
            logger.warning(f"Process pool submit failed ({e}); sequential")
            return [_parse_section_worker(t) for t in tasks]

        results: list[dict | None] = [None] * len(tasks)
        for fut in as_completed(future_map):
            i = future_map[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = {"error": str(e)}
        return results

    def _run_grouped(self, pdf_path: Path, split: SplitResult) -> dict:
        schemes: dict[str, dict] = {}
        results = self._dispatch(split.sections)
        for sec, res in zip(split.sections, results):
            scheme = res.get("scheme")
            if not isinstance(scheme, dict):
                continue
            name = scheme.get("fund_name") or scheme.get("scheme_name") or sec.scheme_name
            if not name:
                continue
            existing = schemes.get(name)
            if existing:
                existing["holdings"].extend(scheme.get("holdings", []))
                if not existing["date"]:
                    existing["date"] = scheme.get("date", "")
            else:
                schemes[name] = scheme

        logger.info(
            f"Grouped {pdf_path.name}: {len(schemes)} schemes across "
            f"{len(split.sections)} sections"
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
            "raw_text": "\n\n".join(split.page_texts),
            "raw_tables": [],
        }

    def _run_flat(self, pdf_path: Path, split: SplitResult,
                  legacy_fallback) -> dict:
        from src.pdf_parser import (
            _classify_tables,
            _clean_tables,
            _empty_result,
            _extract_metadata,
        )

        results = self._dispatch(split.sections)
        raw_tables: list[list[list]] = []
        texts: list[str] = []
        for res in results:
            if res.get("error"):
                continue
            t = res.get("text", "")
            if t:
                texts.append(t)
            raw_tables.extend(res.get("tables", []))

        if not texts and not raw_tables:
            return legacy_fallback()

        result = _empty_result(str(pdf_path), "pdf")
        raw_text = "\n\n".join(t for t in texts if t)
        result["raw_text"] = raw_text
        result["metadata"] = _extract_metadata(raw_text)
        result["raw_tables"] = _clean_tables(raw_tables)
        parsed = _classify_tables(result["raw_tables"])
        result["equity_holdings"] = parsed.get("equity", [])
        result["debt_holdings"] = parsed.get("debt", [])
        result["sector_allocation"] = parsed.get("sector", [])
        result["top_holdings"] = parsed.get("top_holdings", [])
        result["cash_allocation"] = parsed.get("cash")
        return result
