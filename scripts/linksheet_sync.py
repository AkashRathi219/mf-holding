"""AMC link-sheet sync: visit the user-curated links in
``output/amc_document_links.xlsx`` sheet "AMC link sheet" (sheet 3), download
portfolio/factsheet documents published in the trailing window (default: last
3 completed months) and report how the new data compares with the current
raw-data status under ``data/raw/pdfs/<AMC>/``.

Downloads land in their own tree (``data/raw/linksheet/<AMC>/<YYYY>/<MM>/``)
so the main pipeline's ``data/raw/pdfs`` stays untouched; the comparison report
flags which documents are genuinely new vs already covered.

Usage (from ``mf_holding``)::

    python scripts/linksheet_sync.py                    # discover + download + compare
    python scripts/linksheet_sync.py --dry-run          # discover + compare only
    python scripts/linksheet_sync.py --amc bajaj        # one AMC
    python scripts/linksheet_sync.py --months 4 --no-playwright

Outputs:
    output/linksheet_comparison.md    human-readable comparison report
    output/linksheet_comparison.csv   one row per AMC
    data/logs/linksheet/<ts>.json     full provenance manifest
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

logger = logging.getLogger("linksheet_sync")

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import src.ai_extract as ai_extract  # noqa: E402  (OpenRouter plumbing reuse)
from src.document_class import classify  # noqa: E402  (AI sidecar stamping)

DEFAULT_XLSX = BASE / "output" / "amc_document_links.xlsx"
PDFS_DIR = BASE / "data" / "raw" / "pdfs"
LINKSHEET_DIR = BASE / "data" / "raw" / "linksheet"
PARSED_DIR = BASE / "data" / "parsed" / "amc_websites"
REPORT_DIR = BASE / "output"
MANIFEST_DIR = BASE / "data" / "logs" / "linksheet"

AI_DEFAULT_MODEL = "z-ai/glm-5.3-flash"   # OpenRouter GLM 5.3 (repo convention)
OC_DEFAULT_MODEL = "opencode/space-bunny-free"   # free via local opencode CLI
OC_ALT_MODELS = ("opencode/fledge-alpha-free", "opencode-go/glm-5.3-flash")
_OC_BATCH_SIZE = 4

DOC_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".csv", ".zip")
EXT_SET = {e.lstrip(".") for e in DOC_EXTENSIONS}
SKIP_HREF = ("javascript:", "mailto:", "tel:", "data:", "#")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

RATE_LIMIT_MSG = "cloudflare_1015_rate_limited"
_RATE_LIMIT_RE = re.compile(
    r"error 1015|you are being rate limited|banned you temporarily", re.I)
_EMBEDDED_URL_RE = re.compile(
    r'https?://[^"\'\s<>]+?\.(?:pdf|xlsx|xls|csv|zip)', re.I)

# Consent gates (e.g. 360 ONE's Agree/Disagree overlay) must be dismissed
# before the site injects its document library into the DOM.
_CONSENT_POS = re.compile(r"^\s*(Agree|I Agree|Agree and Proceed|Accept|"
                          r"Accept All|Got it)\s*$", re.I)
_CONSENT_NEG = re.compile(r"^\s*(Disagree|No|Decline|Reject( All)?)\s*$", re.I)
_CONSENT_HINT_RE = re.compile(r"cookie|consent|disclaimer", re.I)

_PW_DISABLED = False

# Mirrors main._is_relevant_document: keep portfolio/factsheet-style docs,
# drop obvious non-holdings files.
IRRELEVANT = (
    "tracking-error", "tracking error", "market-flash", "market flash",
    "dividend-declaration", "dividend declaration", "addendum", "notice",
    "nfo", "new fund offer", "press-release", "press release",
    "annual-report", "annual report", "statement of additional", "sai",
    "scheme information", "investor-charter", "investor charter",
    "kyc-form", "kyc", "fatca", "crs", "ubo", "stp", "sip", "swp",
    "redemption", "transmission", "tax-reckoner", "tax reckoner",
    "application-form", "application form", "grievance", "policy",
    "circular", "commission", "soft-dollar", "stewardship", "proxy-voting",
    "proxy voting", "valuation-policy", "voting", "custodian",
    "definitions", "glossary", "mis-selling", "unclaimed", "lock-in",
    "non-business", "nri-corner", "iap", "investor-education",
    "complaint", "average aum", "transaction report",
    "kim-sheet", "kim sheet", "term-sheet", "checklist",
    "empanelment", "common application", "dashboard", "update",
)
_DAILY_TXN_RE = re.compile(r"^\d{2}_[a-z]{3}_\d{2}_")   # SEBI daily transaction dumps

MONTH_TOKENS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]

# Axis special case: its downloads page renders the "Factsheet Documents"
# section only through a token-protected CMS API (same flow as
# src/amc_adapters/axis.py - one headless page load captures the Bearer token
# the page itself uses, then months are queried over plain HTTP).
AXIS_PAGE_URL = "https://www.axismf.com/downloads/products"
AXIS_API_URL = "https://www.axismf.com/cms/product/factsheet"
_NUM_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[-._/](\d{1,2})[-._/](\d{2,4})(?!\d)")
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})((?:19|20)\d{2})(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)?\d{2})(?!\d)")


# ---------------------------------------------------------------- sheet input

@dataclass
class SheetAmc:
    name: str
    urls: dict = field(default_factory=dict)   # role -> url (main/portfolio/factsheets)


def _cell_url(cell) -> str | None:
    """Accept pasted-URL text or an Excel hyperlink whose display text is a label."""
    candidates = [cell.value, getattr(cell.hyperlink, "target", None)]
    for candidate in candidates:
        if isinstance(candidate, str):
            s = candidate.strip()
            if s.lower().startswith(("http://", "https://")):
                return s
    return None


def read_link_sheet(xlsx_path: Path, sheet_name: str) -> list[SheetAmc]:
    import openpyxl

    # read_only=False so cells expose .hyperlink (ReadOnlyCell does not);
    # the workbook is small, so the extra load cost is negligible.
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise SystemExit(f"Sheet '{sheet_name}' not in {xlsx_path} "
                         f"(has: {wb.sheetnames})")
    ws = wb[sheet_name]
    amcs: list[SheetAmc] = []
    for row in ws.iter_rows(min_row=2, max_col=4):
        cells = (list(row) + [None] * 4)[:4]
        name = cells[0].value if cells[0] is not None else None
        if not name or not str(name).strip():
            continue
        urls = {}
        for role, cell in zip(("main", "portfolio", "factsheets"), cells[1:]):
            if cell is None:
                continue
            u = _cell_url(cell)
            if u:
                urls[role] = u
        if urls:
            amcs.append(SheetAmc(name=str(name).strip(), urls=urls))
    wb.close()
    return amcs


# ------------------------------------------------------- relevance & dating

def self_date(text: str) -> tuple[int, int] | None | str:
    """(month, year) hidden in a filename/URL, 'undated' when no signal at all,
    None when a signal exists but yields nothing usable."""
    t = text.lower()
    m = _NUM_DATE_RE.search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            if y < 100:
                y += 2000
            return (mo, y)
    # Compact DDMMYYYY without separators ('monthly-portfolio-31082026...');
    # only usable when day-first is unambiguous.
    mc = _COMPACT_DATE_RE.search(t)
    if mc:
        a, b, y = int(mc.group(1)), int(mc.group(2)), int(mc.group(3))
        if a > 12 and 1 <= b <= 12:
            return (b, y)
        if b > 12 and 1 <= a <= 12:
            return (a, y)
    for tok, mnum in MONTH_TOKENS.items():
        idx = t.find(tok)
        if idx == -1:
            continue
        window = t[idx + len(tok): idx + len(tok) + 12]
        ym = re.search(r"((?:19|20)\d{2})", window) or _YEAR_RE.search(window)
        if not ym:
            return None
        y = int(ym.group(1))
        if y < 100:
            y += 2000
        return (mnum, y)
    return "undated"


_KEYWORDS = ("portfolio", "factsheet", "monthly")


def is_relevant(filename: str) -> bool:
    name = (filename or "").lower()
    if not name.endswith(DOC_EXTENSIONS):
        return False
    if _DAILY_TXN_RE.match(name):
        return False
    if "portfolio" in name or "factsheet" in name or "scheme summary" in name:
        return True
    if name.endswith(".zip"):
        return True
    return not any(p in name for p in IRRELEVANT)


def is_relevant_undated(filename: str) -> bool:
    """Same as amc_direct._keep_link: an undated link is only accepted when the
    filename itself looks like a portfolio/factsheet/monthly document."""
    return is_relevant(filename) and any(k in filename.lower() for k in _KEYWORDS)


def month_window(months: int) -> list[tuple[int, int]]:
    """Trailing `months` completed months before the current one (newest first)."""
    now = datetime.now()
    idx_current = now.year * 12 + now.month      # 1-based month index
    out = []
    for i in range(months):
        idx = idx_current - 1 - i                # last completed month, going back
        y, m0 = divmod(idx - 1, 12)
        out.append((y, m0 + 1))
    return out


def fmt_month(y: int, m: int) -> str:
    return f"{y}-{m:02d}"


# ------------------------------------------------------------- discovery

@dataclass
class DocLink:
    url: str
    filename: str
    source_page: str
    source_role: str
    month: int | None
    year: int | None
    undated: bool = False


@dataclass
class PageResult:
    url: str
    role: str
    ok: bool
    method: str = "httpx"
    error: str = ""
    n_doc_links: int = 0


def extract_doc_links(html: str, base_url: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.lower().startswith(SKIP_HREF):
            continue
        if not href.lower().endswith(DOC_EXTENSIONS):
            continue
        out.append((urljoin(base_url, href), (a.get_text() or "").strip()))
    return out


def extract_embedded_urls(html: str) -> list[str]:
    """Document URLs embedded anywhere in the rendered DOM (React sites often
    render download cards without <a> anchors - the whole library sits in the
    HTML/hydration payload instead)."""
    if not html:
        return []
    return sorted(set(_EMBEDDED_URL_RE.findall(html.replace("\\/", "/"))))


def _dismiss_consent(page) -> bool:
    """Click the affirmative button of a consent dialog until it disappears.
    Only fires when a negative counterpart exists or the page mentions
    cookies/consent, so ordinary pages are never touched."""
    clicked = False
    try:
        for _ in range(6):
            pos = page.locator("button").filter(has_text=_CONSENT_POS)
            if not pos.count():
                break
            neg = page.locator("button").filter(has_text=_CONSENT_NEG)
            if not neg.count():
                body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
                if not _CONSENT_HINT_RE.search(body or ""):
                    break
            try:
                pos.first.click(timeout=2500)
                clicked = True
            except Exception:
                break
            page.wait_for_timeout(2500)
    except Exception:
        pass
    return clicked


async def fetch_static(client, url: str) -> tuple[str, str]:
    """Returns (html, error)."""
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}"
        ct = r.headers.get("content-type", "")
        if ct and "html" not in ct and "xml" not in ct:
            return "", f"non-HTML content-type: {ct}"
        if _RATE_LIMIT_RE.search(r.text):
            return "", RATE_LIMIT_MSG
        return r.text, ""
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


def fetch_playwright(url: str, timeout_s: float) -> tuple[str, str]:
    """Headless-Chromium fallback for JS-heavy pages: dismiss any consent
    dialog, then return the full rendered DOM. Runs in a worker thread (sync
    API cannot run inside a live asyncio loop)."""
    if _PW_DISABLED:
        return "", "playwright disabled by --no-playwright"
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return "", f"playwright unavailable: {exc}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=UA,
                                        viewport={"width": 1366, "height": 900})
                page.goto(url, timeout=int(timeout_s * 1000), wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                html = page.evaluate("() => document.documentElement.innerHTML")
                if _RATE_LIMIT_RE.search(html):
                    return "", RATE_LIMIT_MSG
                if _dismiss_consent(page):
                    page.wait_for_timeout(3500)   # let the site inject its data
                    html = page.evaluate("() => document.documentElement.innerHTML")
                return html, ""
            finally:
                browser.close()
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------- current status

def safe_amc(name: str) -> str:
    return name.replace(" ", "_").replace("/", "-")


class CurrentTree:
    """Snapshot of data/raw/pdfs for the comparison."""

    def __init__(self) -> None:
        self.by_amc: dict[str, dict[tuple[int, int], list[str]]] = {}
        self._norm: dict[str, str] = {}
        if PDFS_DIR.exists():
            for amc_dir in PDFS_DIR.iterdir():
                if not amc_dir.is_dir():
                    continue
                months: dict[tuple[int, int], list[str]] = {}
                for ydir in amc_dir.iterdir():
                    if not ydir.is_dir() or not ydir.name.isdigit():
                        continue
                    for mdir in ydir.iterdir():
                        if not mdir.is_dir() or not mdir.name.isdigit():
                            continue
                        files = [f.name for f in mdir.iterdir()
                                 if f.is_file() and f.stat().st_size > 0]
                        if files:
                            months[(int(ydir.name), int(mdir.name))] = files
                self.by_amc[amc_dir.name] = months
                self._norm[self._normkey(amc_dir.name)] = amc_dir.name

    @staticmethod
    def _normkey(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def resolve(self, amc_name: str) -> str | None:
        direct = safe_amc(amc_name)
        if direct in self.by_amc:
            return direct
        return self._norm.get(self._normkey(amc_name))

    def months(self, amc_name: str) -> dict[tuple[int, int], list[str]]:
        key = self.resolve(amc_name)
        return self.by_amc.get(key, {}) if key else {}

    def has_file(self, amc_name: str, filename: str) -> bool:
        for files in self.months(amc_name).values():
            if filename in files:
                return True
        return False


# ------------------------------------------------------------- per-AMC flow

@dataclass
class AmcReport:
    amc: str
    pages: list = field(default_factory=list)      # PageResult dicts
    docs_found: int = 0
    docs_new: int = 0
    docs_already_current: int = 0
    docs_undated: int = 0
    downloaded: list = field(default_factory=list)  # result dicts
    download_failed: list = field(default_factory=list)
    current_months: list = field(default_factory=list)
    new_months: list = field(default_factory=list)
    gained_months: list = field(default_factory=list)
    missing_months: list = field(default_factory=list)
    latest_before: str = ""
    latest_after: str = ""
    verdict: str = ""
    parsed_months: list = field(default_factory=list)   # pipeline-parsed months (window)
    ai: dict = field(default_factory=dict)


def _page_key(url: str) -> str:
    p = urlsplit(url)
    return (p.netloc.lower(), p.path, p.query)


def _collect_page_docs(url: str, role: str, raw_links: list, docs: dict, window: set) -> int:
    """Filter raw anchors to relevant in-window documents, add to `docs`,
    return how many were added."""
    added = 0
    for url_doc, _text in raw_links:
        fname = unquote(urlsplit(url_doc).path.rsplit("/", 1)[-1]) or "document"
        hint = self_date(f"{fname} {url_doc.lower()}")
        if hint == "undated":
            # no date signal anywhere: only portfolio/factsheet-style names
            # qualify (forms/KIMs/policies would flood the report)
            if not is_relevant_undated(fname):
                continue
        elif not is_relevant(fname):
            continue
        m = y = None
        undated = False
        if hint is None:
            continue            # ambiguous date signal - not trustworthy
        if hint == "undated":
            undated = True
        else:
            m, y = hint
            if (y, m) not in window:
                continue
        if url_doc not in docs:
            docs[url_doc] = DocLink(url=url_doc, filename=fname,
                                    source_page=url, source_role=role,
                                    month=m, year=y, undated=undated)
            added += 1
    return added


async def discover_amc(sheet_amc: SheetAmc, window: set, client, args) -> tuple[list[DocLink], list[PageResult]]:
    docs: dict[str, DocLink] = {}
    pages: list[PageResult] = []
    seen_pages: set = set()
    for role in ("portfolio", "factsheets", "main"):
        url = sheet_amc.urls.get(role)
        if not url:
            continue
        key = _page_key(url)
        if key in seen_pages:
            continue
        seen_pages.add(key)

        html, err = await fetch_static(client, url)
        raw_links = extract_doc_links(html, url) if html else []
        raw_links += [(u, "") for u in extract_embedded_urls(html)]
        res = PageResult(url=url, role=role, ok=bool(html), error=err,
                         n_doc_links=len(raw_links))
        added = _collect_page_docs(url, role, raw_links, docs, window)

        if not added and err != RATE_LIMIT_MSG:
            # Either the static fetch failed or the page rendered zero
            # relevant documents - JS-heavy sites hide the monthly docs
            # behind client-side tabs, so retry with headless Chromium.
            html2, pw_err = await asyncio.to_thread(fetch_playwright, url, args.timeout)
            pw_links = extract_doc_links(html2, url) if html2 else []
            pw_links += [(u, "") for u in extract_embedded_urls(html2)]
            if html2:
                res.method, res.ok, res.error = "playwright", True, ""
                res.n_doc_links = len(pw_links)
                added += _collect_page_docs(url, role, pw_links, docs, window)
            else:
                res.method = "playwright"
                res.error = pw_err or err
        pages.append(res)

        await asyncio.sleep(1.2)  # polite gap between pages

    if not docs and "axis" in sheet_amc.name.lower():
        api_docs = await axis_api_docs(sheet_amc.name, window, args.timeout)
        for d in api_docs:
            docs.setdefault(d.url, d)
        pages.append(PageResult(url=AXIS_API_URL, role="factsheet-api",
                                ok=bool(api_docs), method="axis-api",
                                n_doc_links=len(api_docs)))

    return list(docs.values()), pages


def _capture_axis_token(page_url: str, wait_ms: int) -> str | None:
    """One headless page load; the site itself calls /cms/product/factsheet
    with an Authorization header which we intercept."""
    if _PW_DISABLED:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        logger.warning("Axis: playwright unavailable: %s", exc)
        return None
    tokens: list[str] = []

    def on_request(req):
        if "/cms/product/factsheet" in req.url:
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                tokens.append(auth[len("Bearer "):])

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=UA)
                page.on("request", on_request)
                page.goto(page_url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms)
                try:
                    el = page.get_by_text("Factsheet", exact=True).first
                    if el.count():
                        el.click(timeout=3000)
                        page.wait_for_timeout(2000)
                except Exception:
                    pass
            finally:
                browser.close()
    except Exception as exc:
        logger.warning("Axis token page load failed: %s", exc)
    return tokens[0] if tokens else None


async def axis_api_docs(amc_name: str, window: set, timeout: float) -> list[DocLink]:
    """Query Axis's /cms/product/factsheet for every month in the window."""
    token = await asyncio.to_thread(_capture_axis_token, AXIS_PAGE_URL, 8000)
    if not token:
        return []
    import httpx

    headers = {"User-Agent": UA, "Content-Type": "application/json",
               "Authorization": f"Bearer {token}", "Referer": AXIS_PAGE_URL}
    out: list[DocLink] = []
    async with httpx.AsyncClient(verify=False, timeout=timeout, headers=headers) as client:
        for y, m in sorted(window):
            try:
                r = await client.post(AXIS_API_URL,
                                      json={"year": str(y), "month": MONTH_NAMES[m - 1]})
                items = (r.json().get("data") or {}).get("productFactSheetData") or []
            except Exception as exc:
                logger.warning("Axis API %s-%02d failed: %s", y, m, exc)
                continue
            for it in items:
                url = it.get("documentUrl") or ""
                if not url:
                    continue
                name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "document"
                if not name.lower().endswith(DOC_EXTENSIONS):
                    name += ".pdf"      # API factsheet URLs sometimes omit the extension
                out.append(DocLink(url=url, filename=name,
                                   source_page=AXIS_PAGE_URL,
                                   source_role="factsheet-api", month=m, year=y))
            logger.info("Axis API %s-%02d: %d document(s)", y, m, len(items))
    return out


async def download_docs(amc: str, docs: list[DocLink], client, current: CurrentTree, dry_run: bool) -> tuple[list[dict], list[dict]]:
    sem = asyncio.Semaphore(5)
    done: list[dict] = []
    failed: list[dict] = []

    async def fetch_bytes(url: str):
        r = await client.get(url, follow_redirects=True, timeout=90)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r

    async def one(doc: DocLink) -> None:
        nonlocal done, failed
        async with sem:
            label = fmt_month(doc.year, doc.month) if doc.month else "undated"
            if current.has_file(amc, doc.filename):
                done.append({"file": doc.filename, "month": label,
                             "url": doc.url, "status": "already_current"})
                return
            if doc.undated:
                rel = Path(safe_amc(amc)) / "undated" / doc.filename
            else:
                rel = Path(safe_amc(amc)) / f"{doc.year}" / f"{doc.month:02d}" / doc.filename
            target = LINKSHEET_DIR / rel
            if dry_run:
                done.append({"file": doc.filename, "month": label,
                             "url": doc.url, "status": "dry_run"})
                return
            if target.exists() and target.stat().st_size > 0:
                done.append({"file": doc.filename, "month": label,
                             "url": doc.url, "path": str(rel),
                             "status": "cached"})
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            last_err = ""
            for attempt in (1, 2):
                try:
                    try:
                        r = await fetch_bytes(doc.url)
                    except Exception as exc:
                        if attempt == 1 and ("certificate" in str(exc).lower()
                                             or "ssl" in type(exc).__name__.lower()):
                            async with __import__("httpx").AsyncClient(verify=False, timeout=90) as c2:
                                r = await c2.get(doc.url, follow_redirects=True)
                                if r.status_code >= 400:
                                    raise RuntimeError(f"HTTP {r.status_code}")
                        else:
                            raise
                    name = doc.filename
                    cd = r.headers.get("content-disposition", "")
                    mcd = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                    if mcd:
                        cdname = unquote(mcd.group(1).strip())
                        if cdname.lower().endswith(DOC_EXTENSIONS):
                            name = cdname
                    if not name.lower().endswith(DOC_EXTENSIONS):
                        name += ".bin"
                    target = target.with_name(name)
                    target.write_bytes(r.content)
                    done.append({"file": name, "month": label, "url": doc.url,
                                 "path": str(target.relative_to(LINKSHEET_DIR)),
                                 "bytes": len(r.content), "status": "downloaded"})
                    return
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    if attempt == 1:
                        await asyncio.sleep(1.5)
            failed.append({"file": doc.filename, "url": doc.url, "error": last_err})

    await asyncio.gather(*(one(d) for d in docs))
    return done, failed


def _month_index(t: tuple[int, int]) -> int:
    return t[0] * 12 + t[1]


# ------------------------------------------------------------- AI extraction
# GLM 5.3 via OpenRouter (repo convention: z-ai/glm-5.3-flash), reusing the
# prompts/provider plumbing of src/ai_extract.py. Each document is billed at
# most once: results are cached in a `.ai.json` sidecar next to the file.

def _ai_cfg(args) -> dict:
    backend = getattr(args, "backend", "opencode")
    default_model = OC_DEFAULT_MODEL if backend == "opencode" else AI_DEFAULT_MODEL
    cfg = ai_extract.load_cfg()
    cfg.update({"enabled": True, "model": args.model or default_model,
                "backend": backend,
                "mode": "image", "max_pages": args.ai_pages,
                "max_tokens": 16000})
    if backend == "openrouter" and not ai_extract.is_configured(cfg):
        raise SystemExit("OpenRouter backend needs OPENROUTER_API_KEY in the environment")
    if backend == "opencode" and not _oc_available():
        raise SystemExit("opencode CLI not found on PATH for the opencode backend")
    return cfg


def _ai_schemes_from_raw(raw: str) -> tuple[list[dict], int]:
    """Parse the model JSON into per-scheme summaries (keeps scheme grouping,
    unlike ai_extract._parse_response which flattens to rows). Tolerates CLI
    chatter around the JSON by decoding the first complete object."""
    text = (raw or "").strip()
    doc = None
    start = text.find("{")
    while start != -1 and doc is None:
        try:
            doc, _ = json.JSONDecoder().raw_decode(text[start:])
            break
        except Exception:
            start = text.find("{", start + 1)
    if not isinstance(doc, dict):
        return [], 0
    items = doc.get("schemes") if isinstance(doc.get("schemes"), list) \
        else doc.get("holdings") if isinstance(doc.get("holdings"), list) \
        else ([doc] if doc.get("holdings") or doc.get("name") else [])
    schemes: list[dict] = []
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        rows: list[dict] = []
        hold = item.get("holdings")
        for h in hold if isinstance(hold, list) else []:
            if not isinstance(h, dict):
                continue
            name = str(h.get("company") or h.get("name") or "").strip()
            pct = h.get("percent_nav", h.get("weight"))
            if not name or pct is None:
                continue
            try:
                v = float(str(pct).replace("%", "").replace(",", ""))
            except ValueError:
                continue
            if not 0 < v <= 100:
                continue
            isin = str(h.get("isin") or "").strip().upper()
            rows.append({"company": name, "percent_nav": round(v, 3),
                         "isin": isin if len(isin) == 12 else ""})
        if not rows and not item.get("name"):
            continue
        rows.sort(key=lambda r: -r["percent_nav"])
        psum = round(sum(r["percent_nav"] for r in rows), 1)
        schemes.append({
            "name": str(item.get("name") or "").strip(),
            "date": str(item.get("date") or "").strip(),
            "n_holdings": len(rows), "pct_sum": psum,
            "valid": (max((r["percent_nav"] for r in rows), default=0) <= 100
                      and psum <= 120),
            "top5": rows[:5],
        })
        total += len(rows)
    return schemes, total


_HOLDINGS_KW_RE = re.compile(
    r"%\s*to\s*(?:nav|aum)|% of (?:nav|aum)|equity holdings|debt holdings|"
    r"instrument|market value|book value|isin|name of the instrument", re.I)
_ISIN_RE = re.compile(r"\bIN[EZ][0-9A-Z]{9}\b")
_PCT_LINE_RE = re.compile(r"\d{1,2}\.\d{1,2}\s*%")


def _ai_chat(cfg: dict, messages: list[dict]) -> str:
    """Local thin wrapper around the OpenRouter chat call so the token cap is
    configurable (src.ai_extract._chat hardcodes 4000, which truncates dense
    portfolio tables). GLM is a reasoning model: when its thinking consumes
    the whole budget the response arrives with EMPTY content - retry those."""
    import httpx as _httpx

    last_err: Exception | None = None
    empty_retries = 0
    with _httpx.Client(timeout=cfg.get("timeout", 120)) as client:
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            nudge = ""
            if empty_retries:
                nudge = ("\n\nIMPORTANT: Respond ONLY with the requested JSON "
                         "object. No explanations, no preamble.")
            payload = {
                "model": cfg["model"], "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": int(cfg.get("max_tokens", 16000)),
            }
            if nudge:
                payload["messages"] = messages + [
                    {"role": "user", "content": nudge}]
            try:
                r = client.post(f"{cfg['base_url']}/chat/completions",
                                headers=ai_extract._headers(cfg), json=payload)
                if r.status_code >= 500 or r.status_code == 429:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    continue
                r.raise_for_status()
                data = r.json()
                content = (data.get("choices") or [{}])[0].get(
                    "message", {}).get("content", "")
                usage = data.get("usage") or {}
                logger.info("AI extract: model=%s tokens=%s",
                            cfg["model"], usage.get("total_tokens"))
                if content:
                    return content
                empty_retries += 1
                if empty_retries >= 2:
                    return ""
                last_err = RuntimeError("empty model content (reasoning ate the budget)")
            except _httpx.HTTPStatusError as e:
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise ai_extract.ExtractError(
                        f"provider rejected request: HTTP {e.response.status_code} "
                        f"{e.response.text[:200]}") from e
                last_err = e
            except (_httpx.TransportError, _httpx.TimeoutException) as e:
                last_err = e
    raise ai_extract.ExtractError(f"AI provider unreachable: {last_err}")


# ------------------------------------------------- opencode CLI backend (free)
# Same near-zero-cost tier src/financial_statements.py uses: shell out to the
# local `opencode run -m <model>` CLI (auth via the user's opencode login, no
# OpenRouter credits). "Space Bunny" / "Fledge Alpha" are opencode's free
# models; prompts point the agent at rendered PNG pages / dumped sheet text.

_OC_PROMPT = (
    "You are a precise financial-data extraction engine for Indian mutual fund "
    "monthly portfolio disclosures. Read the attached files and extract every "
    "scheme's portfolio holdings EXACTLY as printed. Rules: output ONLY a JSON "
    "object, no prose; percent_nav is the number printed in the '% to NAV' or "
    "'% of AUM' column (float, no % sign), skip rows without one; keep "
    "company/instrument names verbatim; do NOT invent ISINs, include them only "
    "when printed; include every scheme found with its 'name' and 'date' when "
    "visible; ignore performance tables, fund managers, disclaimers, "
    "footnotes. Schema: {\"schemes\": [{\"name\": str, \"date\": str, "
    "\"holdings\": [{\"company\": str, \"percent_nav\": number, "
    "\"isin\": str|null}]}]}"
)

_OC_TMP = LINKSHEET_DIR / "_oc_tmp"


def _oc_available() -> bool:
    try:
        shutil_which_opencode()
        return True
    except ai_extract.ExtractError:
        return False


def shutil_which_opencode() -> str:
    """Resolve the REAL opencode executable (mirrors
    src/financial_statements._opencode_command): the npm .cmd/.ps1 shim on
    PATH cannot be executed by subprocess directly and garbles multi-line
    arguments, so map it to the sibling node_modules/opencode-ai exe."""
    import shutil

    exe = shutil.which("opencode")
    if not exe:
        raise ai_extract.ExtractError("opencode CLI not found on PATH")
    p = Path(exe)
    if p.name.lower().endswith((".cmd", ".ps1")):
        cand = p.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if cand.exists():
            return str(cand)
    return exe


def _oc_run(prompt: str, model: str, timeout_s: float = 600.0) -> str:
    """One opencode CLI turn. File paths are referenced INSIDE the prompt
    (the agent reads them with its own tools) - the CLI's -f attachment flag
    greedily swallows the positional message and must not be used."""
    import subprocess

    exe = shutil_which_opencode()
    cmd = [exe, "run", "-m", model, prompt]
    proc = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=30)
        except Exception:
            out, err = "", ""
    return (out or "") + "\n" + (err or "")


def _oc_extract(raw_text: str) -> tuple[list[dict], int]:
    return _ai_schemes_from_raw(raw_text)


def _render_png(pdf_path: Path, pages: list[int], outdir: Path) -> list[Path]:
    import pymupdf

    outdir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    doc = pymupdf.open(pdf_path)
    try:
        for i in pages:
            pix = doc[i].get_pixmap(dpi=int(cfg_dpi()))
            p = outdir / f"{pdf_path.stem}-p{i + 1}.png"
            pix.save(p)
            paths.append(p)
    finally:
        doc.close()
    return paths


def cfg_dpi() -> int:
    return 150


def oc_extract_pdf(cfg: dict, pdf_path: Path) -> tuple[list[dict], int]:
    """opencode CLI route for PDFs: render the keyword-ranked pages as PNGs,
    run the free model over them in <=4-page batches, merge scheme dicts."""
    import pymupdf as fitz

    doc = fitz.open(pdf_path)
    try:
        scored = []
        for i in range(doc.page_count):
            text = doc[i].get_text() or ""
            if len(text.strip()) < 40:
                continue
            score = len(_ISIN_RE.findall(text)) * 3 \
                + min(len(_PCT_LINE_RE.findall(text)), 20) \
                + len(_HOLDINGS_KW_RE.findall(text))
            scored.append((score, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        pages = sorted(i for s, i in scored[:max(1, int(cfg.get("max_pages", 6)))] if s > 0)
        if not pages:
            pages = list(range(min(doc.page_count, int(cfg.get("max_pages", 6)))))
    finally:
        doc.close()

    png_dir = _OC_TMP / pdf_path.stem
    pngs = _render_png(pdf_path, pages, png_dir)
    schemes_out: list[dict] = []
    total = 0
    for bstart in range(0, len(pngs), _OC_BATCH_SIZE):
        batch = pngs[bstart:bstart + _OC_BATCH_SIZE]
        rel = "; ".join(str(p.relative_to(BASE)) for p in batch)
        prompt = (f"{_OC_PROMPT}\nFILES (read each image, then transcribe the "
                  f"holdings tables): {rel}\nReturn ONLY the JSON object.")
        raw = ""
        for attempt in range(2):
            raw = _oc_run(prompt, cfg["model"])
            schemes, _nh = _oc_extract(raw)
            if schemes:
                break
        schemes, _nh = _oc_extract(raw)
        have = {s["name"].lower() for s in schemes_out if s["name"]}
        for s in schemes:
            if s["name"].lower() not in have:
                schemes_out.append(s)
                total += s["n_holdings"]
    return schemes_out, total


def oc_extract_sheet(cfg: dict, path: Path) -> tuple[list[dict], int]:
    """opencode CLI route for spreadsheets: dump the keyword-ranked sheets to
    a text file the agent can read, ask for the JSON in one run."""
    import pandas as pd

    xl = pd.ExcelFile(path)
    scored = []
    for name in xl.sheet_names:
        try:
            head = xl.parse(name, nrows=25)
        except Exception:
            continue
        text = head.to_csv(index=False).lower()
        score = len(_SHEET_KW_RE.findall(text)) + len(_ISIN_RE.findall(text)) * 3
        if score > 0:
            scored.append((score, name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    picks = [name for _, name in scored[:3]] or xl.sheet_names[:2]
    tables = []
    for name in picks:
        df = xl.parse(name, nrows=50)
        tables.append(f"## Sheet: {name}\n" + df.head(50).to_csv(index=False))
    text = "\n\n".join(tables)[:14000]

    _OC_TMP.mkdir(parents=True, exist_ok=True)
    txt = _OC_TMP / f"{path.stem}-sheet.txt"
    txt.write_text(text, encoding="utf-8")
    rel = str(txt.relative_to(BASE))
    prompt = (f"{_OC_PROMPT}\nFILE (read it; it holds flattened CSV tables of "
              f"a monthly portfolio disclosure, one table per scheme): {rel}\n"
              "Return ONLY the JSON object.")
    raw = ""
    for attempt in range(2):
        raw = _oc_run(prompt, cfg["model"])
        schemes, nh = _oc_extract(raw)
        if schemes:
            break
    return _oc_extract(raw)


_SHEET_KW_RE = re.compile(r"isin|portfolio|holding|% to nav|instrument|"
                          r"name of the instrument|company", re.I)


def ai_extract_pdf(cfg: dict, pdf_path: Path) -> tuple[list[dict], int]:
    import pymupdf as fitz

    doc = fitz.open(pdf_path)
    try:
        n_pages = doc.page_count
        # Smart page selection: ISIN-bearing rows are the strongest signal of
        # a holdings table; generic keywords are the weaker fallback.
        scored = []
        for i in range(n_pages):
            text = doc[i].get_text() or ""
            if len(text.strip()) < 40:      # blank/cover page
                continue
            score = len(_ISIN_RE.findall(text)) * 3 \
                + min(len(_PCT_LINE_RE.findall(text)), 20) \
                + len(_HOLDINGS_KW_RE.findall(text))
            scored.append((score, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        pages = sorted(i for score, i in scored[:max(1, int(cfg.get("max_pages", 6)))] if score > 0)
        if not pages:                       # no text layer / no hits: first N
            pages = list(range(min(n_pages, int(cfg.get("max_pages", 6)))))
        page_texts = [(doc[i].get_text() or "").strip() for i in pages]
        total_text = sum(len(t) for t in page_texts)
        if total_text > 1500:
            # Text-first: the pages have a usable text layer - dense numeric
            # tables parse far more reliably (and cheaper) as text than as
            # page images. Vision stays as the fallback for scanned PDFs.
            joined = ""
            for t in page_texts:
                if len(joined) + len(t) > 20000:
                    break
                joined += "\n\n" + t
            content = [{"type": "text",
                        "text": ai_extract.USER_PROMPT
                        + "\n\nFACTSHEET TEXT (pages may repeat headers; extract "
                          "every instrument row with its % to NAV):\n" + joined}]
            raw = _ai_chat(cfg, [
                {"role": "system", "content": ai_extract.SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ])
            return _ai_schemes_from_raw(raw)
        images = [ai_extract._page_image_b64(pdf_path, doc[i], int(cfg.get("dpi", 150)))
                  for i in pages]
    finally:
        doc.close()
    content = [{"type": "text", "text": ai_extract.USER_PROMPT}]
    content += [{"type": "image_url", "image_url": {"url": b}} for b in images]
    raw = _ai_chat(cfg, [
        {"role": "system", "content": ai_extract.SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ])
    return _ai_schemes_from_raw(raw)


def ai_extract_sheet(cfg: dict, path: Path) -> tuple[list[dict], int]:
    """Spreadsheet route: keyword-rank sheets/tables (consolidated disclosure
    workbooks have one sheet per scheme - the first sheets are index/cover),
    flatten the best ones to CSV text, ask the model in text mode."""
    import pandas as pd

    _SHEET_KW = re.compile(r"isin|portfolio|holding|% to nav|instrument|"
                           r"name of the instrument|company", re.I)
    tables: list[str] = []
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, nrows=40)
        tables.append("## Sheet: 1\n" + df.to_csv(index=False))
    else:
        xl = pd.ExcelFile(path)
        scored = []
        for name in xl.sheet_names:
            try:
                head = xl.parse(name, nrows=25)
            except Exception:
                continue
            text = head.to_csv(index=False).lower()
            score = len(_SHEET_KW.findall(text)) + len(_ISIN_RE.findall(text)) * 3
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda t: (-t[0], t[1]))
        picks = [name for _, name in scored[:3]] or xl.sheet_names[:2]
        for name in picks:
            df = xl.parse(name, nrows=50)
            tables.append(f"## Sheet: {name}\n" + df.head(50).to_csv(index=False))
    text = "\n\n".join(tables)[:14000]
    content = [{"type": "text",
                "text": ai_extract.USER_PROMPT + "\n\nDOCUMENT TEXT:\n" + text}]
    raw = _ai_chat(cfg, [
        {"role": "system", "content": ai_extract.SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ])
    return _ai_schemes_from_raw(raw)


def current_parsed_months(amc: str) -> list[tuple[int, int]]:
    """Months already parsed by the main pipeline for this AMC."""
    root = PARSED_DIR / safe_amc(amc)
    if not root.exists():
        # fall back to normalized dir matching
        if PARSED_DIR.exists():
            want = re.sub(r"[^a-z0-9]", "", amc.lower())
            for d in PARSED_DIR.iterdir():
                if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == want:
                    root = d
                    break
    out: list[tuple[int, int]] = []
    if not root.exists():
        return out
    for ydir in root.iterdir():
        if not ydir.is_dir() or not ydir.name.isdigit():
            continue
        for mdir in ydir.iterdir():
            if mdir.is_dir() and mdir.name.isdigit() and any(mdir.iterdir()):
                out.append((int(ydir.name), int(mdir.name)))
    return sorted(out, key=_month_index)


def run_ai_step(manifest_amcs: list[dict], args) -> dict:
    """GLM 5.3 extraction for every downloaded (non-already-current) document.
    ZIP archives are unpacked and their portfolio sheets processed. Results
    land as .ai.json sidecars; cached across runs."""
    cfg = _ai_cfg(args)
    summary = {"model": cfg["model"], "processed": 0, "skipped_cached": 0,
               "failed": [], "amcs": {}}

    def process_sheet(fpath: Path, month_label: str, per: dict, cached: bool) -> None:
        sidecar = fpath.parent / "parsed" / f"{fpath.name}.ai.json"
        schemes: list[dict] = []
        nh = 0
        if cached and sidecar.exists():
            try:
                prev = json.loads(sidecar.read_text(encoding="utf-8"))
                schemes = prev.get("schemes", [])
                nh = prev.get("n_holdings", 0)
            except Exception:
                schemes = []
        if not schemes:
            if cfg.get("backend") == "opencode":
                schemes, nh = oc_extract_sheet(cfg, fpath)
            else:
                schemes, nh = ai_extract_sheet(cfg, fpath)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps({
                "model": cfg["model"], "file": fpath.name,
                "amc": per["_amc"], "month": month_label,
                "document_class": classify({"file": fpath.name, "schemes": schemes}),
                "schemes": schemes, "n_holdings": nh,
                "extracted_at": datetime.now().isoformat(timespec="seconds"),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            summary["processed"] += 1
        per["docs"].append({"file": str(fpath.relative_to(LINKSHEET_DIR)),
                            "cached": cached and bool(schemes), "schemes": schemes,
                            "n_holdings": nh, "month": month_label})
        per["schemes"] += len(schemes)
        per["holdings"] += nh

    for entry in manifest_amcs:
        amc = entry["amc"]
        per = {"docs": [], "schemes": 0, "holdings": 0, "failed": [],
               "_amc": amc}
        # Cost guard: prioritize portfolio/factsheet-named docs, then PDFs;
        # cap the number of AI calls per AMC per run.
        candidates = [d for d in entry["download_results"]
                      if d.get("status") not in ("already_current", "dry_run", "")]
        candidates.sort(key=lambda d: (
            0 if any(k in d.get("file", "").lower()
                     for k in ("portfolio", "factsheet", "monthly")) else 1,
            0 if d.get("file", "").lower().endswith(".zip") else 1,
            d.get("file", "").lower()))
        cap = max(1, int(getattr(args, "ai_cap", 20)))
        skipped_by_cap = [d.get("file", "") for d in candidates[cap:]]
        for dres in candidates[:cap]:
            fname = dres.get("file", "")
            month_label = dres.get("month") or "undated"
            if fname.lower().endswith(".zip"):
                import zipfile
                fpath = LINKSHEET_DIR / dres.get("path", "")
                if not fpath.exists():
                    continue
                try:
                    with zipfile.ZipFile(fpath) as zf:
                        members = [n for n in zf.namelist()
                                   if n.lower().endswith((".xlsx", ".xls", ".csv"))
                                   and not n.startswith("__MACOSX")]
                except Exception as exc:
                    per["failed"].append({"file": fname, "error": f"zip unreadable: {exc}"})
                    continue
                if not members:
                    per["failed"].append({"file": fname, "error": "zip without sheet members"})
                    continue
                picks = [m for m in members if "portfolio" in m.lower()] or members
                outdir = fpath.parent / "parsed" / (fpath.stem + "_zip")
                for mname in picks[:3]:
                    dest = outdir / Path(mname).name
                    try:
                        if not dest.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zipfile.ZipFile(fpath) as zf:
                                dest.write_bytes(zf.read(mname))
                        sidecar = dest.parent / "parsed" / f"{dest.name}.ai.json"
                        cached = sidecar.exists()
                        process_sheet(dest, month_label, per, cached)
                    except Exception as exc:
                        per["failed"].append(
                            {"file": f"{fname}::{mname}",
                             "error": f"{type(exc).__name__}: {exc}"})
                continue

            fpath = LINKSHEET_DIR / dres.get("path", "")
            if not fpath.exists():
                continue
            sidecar = fpath.parent / "parsed" / f"{fname}.ai.json"
            ext = Path(fname).suffix.lower()
            if ext not in (".pdf", ".xlsx", ".xls", ".csv"):
                per["failed"].append({"file": fname, "error": "unsupported type"})
                continue
            if sidecar.exists():
                try:
                    prev = json.loads(sidecar.read_text(encoding="utf-8"))
                    per["docs"].append({"file": fname, "cached": True,
                                        "schemes": prev.get("schemes", []),
                                        "n_holdings": prev.get("n_holdings", 0),
                                        "month": month_label})
                    per["schemes"] += len(prev.get("schemes", []))
                    per["holdings"] += prev.get("n_holdings", 0)
                    summary["skipped_cached"] += 1
                    continue
                except Exception:
                    pass
            logger.info("AI extract: %s / %s", amc, fname)
            try:
                if cfg.get("backend") == "opencode":
                    if ext == ".pdf":
                        schemes, nh = oc_extract_pdf(cfg, fpath)
                    else:
                        schemes, nh = oc_extract_sheet(cfg, fpath)
                elif ext == ".pdf":
                    schemes, nh = ai_extract_pdf(cfg, fpath)
                else:
                    schemes, nh = ai_extract_sheet(cfg, fpath)
                sidecar.parent.mkdir(parents=True, exist_ok=True)
                sidecar.write_text(json.dumps({
                    "model": cfg["model"], "file": fname,
                    "amc": amc, "month": month_label,
                    "document_class": classify({"file": fname, "schemes": schemes}),
                    "schemes": schemes, "n_holdings": nh,
                    "extracted_at": datetime.now().isoformat(timespec="seconds"),
                }, indent=2, ensure_ascii=False), encoding="utf-8")
                per["docs"].append({"file": fname, "cached": False,
                                    "schemes": schemes, "n_holdings": nh,
                                    "month": month_label})
                per["schemes"] += len(schemes)
                per["holdings"] += nh
                summary["processed"] += 1
            except Exception as exc:
                per["failed"].append({"file": fname,
                                      "error": f"{type(exc).__name__}: {exc}"})
        per.pop("_amc", None)
        if skipped_by_cap:
            per["skipped_by_cap"] = skipped_by_cap
        entry["ai"] = per
        summary["amcs"][amc] = per
        if per["failed"]:
            summary["failed"] += [f"{amc}:{f['file']}" for f in per["failed"]]
    return summary


# ------------------------------------------------------------- reporting

def build_report(results: list[AmcReport], window: list, args, n_total: int,
                 ai_summary: dict | None = None) -> tuple[str, list[dict]]:
    wlabels = [fmt_month(y, m) for y, m in window]
    lines: list[str] = []
    rows: list[dict] = []

    total_found = sum(r.docs_found for r in results)
    total_new = sum(r.docs_new for r in results)
    total_already = sum(r.docs_already_current for r in results)
    total_dl = sum(1 for r in results for d in r.downloaded if d.get("status") == "downloaded")
    total_fail = sum(len(r.download_failed) for r in results)
    gaining = [r for r in results if r.gained_months]

    lines.append("# AMC link-sheet sync -- comparison report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')} * "
                 f"window: {wlabels[-1]} ... {wlabels[0]} * "
                 f"sheet: {Path(args.xlsx).name} [{args.sheet}]"
                 f"{' * DRY RUN' if args.dry_run else ''}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- AMCs in sheet: {n_total} * with links: {len(results)}")
    lines.append(f"- Relevant documents found in window: {total_found} "
                 f"(not in current data: {total_new}, already in current data: "
                 f"{total_already}, undated: {sum(r.docs_undated for r in results)})")
    lines.append(f"- Downloaded now: {total_dl} * failed: {total_fail}")
    lines.append(f"- AMCs gaining new month(s): {len(gaining)}")
    lines.append("")

    lines.append("## Per-AMC comparison")
    lines.append("")
    lines.append("| AMC | Pages ok | Docs | Not in current | Current months (window) | Site offers | Latest before -> after | Missing in window | Verdict |")
    lines.append("|---|---|---:|---:|---|---|---|---|---|")
    for r in results:
        pg = f"{sum(1 for p in r.pages if p['ok'])}/{len(r.pages)}"
        lines.append(
            f"| {r.amc} | {pg} | {r.docs_found} | {r.docs_new} "
            f"| {', '.join(r.current_months) or '--'} "
            f"| {', '.join(r.new_months) or '--'} "
            f"| {r.latest_before or '--'} -> {r.latest_after or '--'} "
            f"| {', '.join(r.missing_months) or '--'} "
            f"| {r.verdict} |")
        rows.append({
            "amc": r.amc,
            "pages_ok": pg,
            "docs_found_in_window": r.docs_found,
            "docs_not_in_current": r.docs_new,
            "docs_already_current": r.docs_already_current,
            "docs_undated": r.docs_undated,
            "downloaded": sum(1 for d in r.downloaded if d.get("status") == "downloaded"),
            "download_failed": len(r.download_failed),
            "current_months_window": "; ".join(r.current_months),
            "site_offers_months": "; ".join(r.new_months),
            "gained_months": "; ".join(r.gained_months),
            "missing_months": "; ".join(r.missing_months),
            "latest_before": r.latest_before,
            "latest_after": r.latest_after,
            "verdict": r.verdict,
        })

    lines.append("")
    lines.append("## Details")
    for r in results:
        lines.append("")
        lines.append(f"### {r.amc} -- {r.verdict}")
        for p in r.pages:
            status = f"{p['method']} ok" if p["ok"] else f"{p['method']} ERROR: {p['error']}"
            lines.append(f"- Page [{p['role']}] {p['url']} -- {status} ({p['n_doc_links']} doc links)")
        if r.downloaded:
            lines.append(f"- Documents ({len(r.downloaded)}):")
            for d in r.downloaded[:40]:
                lines.append(f"  - [{d.get('month')}] {d['file']} -- {d.get('status')}")
            if len(r.downloaded) > 40:
                lines.append(f"  - ... {len(r.downloaded) - 40} more")
        for f in r.download_failed:
            lines.append(f"- FAILED: {f['file']} -- {f['error']} ({f['url']})")

    if ai_summary is not None:
        lines.append("")
        backend_label = ("opencode CLI (free)" if args.backend == "opencode"
                         else "OpenRouter")
        lines.append(f"## AI extraction ({ai_summary['model']} via {backend_label})")
        lines.append("")
        lines.append(f"Documents processed now: {ai_summary['processed']} · "
                     f"sidecar-cached: {ai_summary['skipped_cached']} · "
                     f"failed: {len(ai_summary['failed'])}")
        lines.append("")
        lines.append("| AMC | AI docs | Schemes | Holdings rows | Current parsed months (window) | AI-extracted months |")
        lines.append("|---|---:|---:|---:|---|---|")
        for r in results:
            ai = r.ai or {}
            docs = ai.get("docs", [])
            ai_months = sorted({d.get("month", "") for d in docs} - {"undated", ""})
            lines.append(
                f"| {r.amc} | {len(docs)} | {ai.get('schemes', 0)} "
                f"| {ai.get('holdings', 0)} "
                f"| {', '.join(r.parsed_months) or '--'} "
                f"| {', '.join(ai_months) or '--'} |")
        lines.append("")
        lines.append("### Per-document AI results")
        for r in results:
            ai = r.ai or {}
            if not ai.get("docs") and not ai.get("failed"):
                continue
            lines.append("")
            lines.append(f"**{r.amc}**")
            for d in ai.get("docs", []):
                src = "cached" if d.get("cached") else "extracted now"
                lines.append(f"- {d['file']} ({src}, {d.get('n_holdings', 0)} holdings)")
                for s in d.get("schemes", [])[:8]:
                    top = "; ".join(f"{t['company']} {t['percent_nav']:g}%" for t in s.get("top5", [])[:3])
                    lines.append(f"  - {s.get('name') or '(unnamed scheme)'} -- "
                                 f"{s['n_holdings']} rows, sum {s['pct_sum']:g}%"
                                 f"{'' if s['valid'] else '  [CHECK: sum>120]'}"
                                 f"{' · top: ' + top if top else ''}")
                if len(d.get("schemes", [])) > 8:
                    lines.append(f"  - ... {len(d['schemes']) - 8} more schemes")
            for f in ai.get("failed", []):
                lines.append(f"- AI FAILED: {f['file']} -- {f['error']}")
    lines.append("")
    return "\n".join(lines), rows


# ------------------------------------------------------------- main

async def run(args) -> int:
    amcs = read_link_sheet(args.xlsx, args.sheet)
    if args.amc:
        amcs = [a for a in amcs if args.amc.lower() in a.name.lower()]
    if not amcs:
        logger.error("No AMCs with links matched")
        return 1

    window = month_window(args.months)
    window_set = {(y, m) for y, m in window}
    current = CurrentTree()
    logger.info("Window %s ... %s * %d AMC(s) with links",
                fmt_month(*window[-1]), fmt_month(*window[0]), len(amcs))

    import httpx

    results: list[AmcReport] = []
    manifest_amcs: list[dict] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"},
        timeout=args.timeout,
    ) as client:
        for sheet_amc in amcs:
            logger.info("-> %s", sheet_amc.name)
            docs, pages = await discover_amc(sheet_amc, window_set, client, args)
            done, failed = await download_docs(sheet_amc.name, docs, client, current, args.dry_run)

            r = AmcReport(amc=sheet_amc.name)
            r.pages = [p.__dict__ for p in pages]
            r.docs_found = len(docs)
            r.docs_undated = sum(1 for d in docs if d.undated)
            r.docs_new = sum(1 for d in done if d.get("status") != "already_current")
            r.docs_already_current = sum(1 for d in done if d.get("status") == "already_current")
            r.downloaded = done
            r.download_failed = failed

            cur_months = sorted(current.months(sheet_amc.name), key=_month_index)
            r.current_months = [fmt_month(y, m) for y, m in cur_months if (y, m) in window_set]
            new_month_set = {(d.year, d.month) for d in docs if d.month}
            r.new_months = [fmt_month(y, m) for y, m in sorted(new_month_set, key=_month_index)
                            if (y, m) in window_set]
            cur_set = set(cur_months)
            r.gained_months = [mm for mm in r.new_months
                               if (int(mm[:4]), int(mm[5:7])) not in cur_set]
            r.missing_months = [fmt_month(y, m) for y, m in window
                                if (y, m) not in cur_set and (y, m) not in new_month_set]
            r.latest_before = fmt_month(*cur_months[-1]) if cur_months else ""
            after = sorted(cur_set | new_month_set, key=_month_index)
            r.latest_after = fmt_month(*after[-1]) if after else ""

            if not sheet_amc.urls:
                r.verdict = "no link in sheet"
            elif r.pages and all(not p["ok"] for p in r.pages):
                r.verdict = "page error"
            elif not docs:
                r.verdict = "no relevant docs in window"
            elif r.gained_months:
                r.verdict = "NEW month(s): " + ", ".join(r.gained_months)
            elif r.docs_new:
                r.verdict = "new files (months already covered)"
            else:
                r.verdict = "already covered"
            results.append(r)
            manifest_amcs.append({
                "amc": sheet_amc.name, "urls": sheet_amc.urls,
                "pages": r.pages,
                "docs": [d.__dict__ for d in docs],
                "download_results": done + failed,
                "comparison": {
                    "current_months": r.current_months, "new_months": r.new_months,
                    "gained_months": r.gained_months, "missing_months": r.missing_months,
                    "latest_before": r.latest_before, "latest_after": r.latest_after,
                    "verdict": r.verdict,
                },
            })
            await asyncio.sleep(1.5)  # politeness between AMCs

    ai_summary = None
    if args.ai and not args.dry_run:
        cfg_probe = _ai_cfg(args)
        logger.info("AI extraction via %s model=%s",
                    cfg_probe["backend"], cfg_probe["model"])
        ai_summary = run_ai_step(manifest_amcs, args)
        for entry, r in zip(manifest_amcs, results):
            r.ai = entry.get("ai") or {}
            r.parsed_months = [fmt_month(y, m) for y, m in current_parsed_months(r.amc)
                               if (y, m) in window_set]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md, rows = build_report(results, window, args, len(amcs), ai_summary)
    (REPORT_DIR / "linksheet_comparison.md").write_text(md, encoding="utf-8")
    with open(REPORT_DIR / "linksheet_comparison.csv", "w", newline="", encoding="utf-8") as fh:
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "xlsx": str(args.xlsx), "sheet": args.sheet,
        "window": [fmt_month(y, m) for y, m in window],
        "dry_run": args.dry_run,
        "amcs": manifest_amcs,
    }
    mpath = MANIFEST_DIR / f"linksheet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Report: %s * %s * manifest: %s",
                REPORT_DIR / "linksheet_comparison.md",
                REPORT_DIR / "linksheet_comparison.csv", mpath)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Download AMC link-sheet documents for the "
                                             "last N months and compare with current data")
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--sheet", default="AMC link sheet")
    ap.add_argument("--months", type=int, default=3,
                    help="trailing completed months to cover (default 3)")
    ap.add_argument("--amc", default=None, help="case-insensitive substring filter")
    ap.add_argument("--dry-run", action="store_true", help="discover + compare, do not write files")
    ap.add_argument("--no-playwright", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--ai", action="store_true",
                    help="extract holdings from downloaded docs (AI tier)")
    ap.add_argument("--backend", choices=("opencode", "openrouter"),
                    default="opencode",
                    help="AI provider: local opencode CLI free models (default) "
                         "or OpenRouter API (uses credits)")
    ap.add_argument("--model", default=None,
                    help=f"model id (default: {OC_DEFAULT_MODEL} for opencode "
                         f"backend, {AI_DEFAULT_MODEL} for openrouter)")
    ap.add_argument("--ai-pages", type=int, default=6,
                    help="max PDF pages sent to the model per document (keyword-ranked)")
    ap.add_argument("--ai-cap", type=int, default=20,
                    help="max AI-processed documents per AMC per run (cost guard)")
    args = ap.parse_args()
    if args.no_playwright:
        global _PW_DISABLED
        _PW_DISABLED = True
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
