# AMC Websites — Fetching & Scraping Map

The monthly portfolio/holdings pipeline. 57 AMCs are tracked in
`config/amc_registry.json`; 20 have dedicated adapters, the rest use
`HybridAdapter` (plain HTTP → Playwright fallback). Selection is by exact
`mf_name` string match (`src/amc_adapters/__init__.py:84-88`).

---

## 1. End-to-end flow

Two entry routes, one download/parse core:

**Route A — registry-driven monthly pipeline** (`python main.py run`, scheduler job
`monthly_holdings_fetch`, days 1–5 at 06:00 IST with retry until success marker):

```
config/amc_registry.json
  → get_adapter(mf_name)
  → adapter.discover_documents()          # async link discovery
  → recency filter (±2 months) + relevance filter
  → DocumentDownloader.download_all()     # httpx, curl_cffi fallback
  → sha256 parse-cache check
  → src/batch_parser.batch_parse()        # process pool
  → save_parsed()                         # JSON + CSV
  → report_YYYY_MM.json
```

**Route B — link-first capture** (monthly re-download cycle):

```
python main.py portfolio-links      # save_links(): per-AMC discovery → manifest
  → data/logs/portfolio_links/<YYYY-MM>.json
python main.py portfolio-download   # download_saved(): manifest → pdfs dir by self-detected month
  → then parse-batch / ingest
```

Attribution: files parsed here become holdings with `source='amc_website'` in the
webapp — priority `amfi > amc_website > advisorkhoj > index`
(`webapp/db.py:1153`).

---

## 2. Registry — `config/amc_registry.json`

- 57 AMC entries: `mf_id`, `mf_name`, `amc_monthly_mf_factsheets`,
  `amc_monthly_portfolio_disclosure`, `amc_fortnightly_portfolio_disclosure`,
  `scheme_wise`; optional `schemes[]` and `amfi_directory_url`.
- The pipeline consumes portfolio + factsheet URLs only; adapter selection is by
  `mf_name`.
- Verified/filled monthly by `webapp/amfi_portal.refresh_registry()` from the
  official AMFI directory (see [amfi.md](amfi.md) §5.1).

---

## 3. Dedicated adapters (20)

| Adapter | AMC | Site / endpoint (verified) | Discovers | Method / special handling |
|---|---|---|---|---|
| `icici.py` | ICICI Prudential | `digitalfactsheet.icicipruamc.com/fact/` + `/passive/`; PDFs `<base>pdf/<slug>.pdf` | Per-scheme digital factsheets (current month) | httpx + regex; scheme look-alike pages excluded |
| `navi.py` | Navi | `navi.com/mutual-fund/downloads/portfolio` | Scheme-wise monthly portfolios by FY/month selectors | Playwright select interaction (2 FYs × 12 months) |
| `pgim.py` | PGIM India | `POST pgimindia.com/api/v1/brochure/published/disclosure` (`headerId=2`, `sectionId=SECTION_747960037`) | Monthly portfolio XLSX | httpx JSON API |
| `choice.py` | Choice | `POST choicemf.com/api/monthly-portfolio-report/portfolio-website-list` | Monthly portfolio reports + report_date | httpx JSON; files from `doc.choicemf.com` |
| `jio.py` | Jio BlackRock | `jioblackrockamc.com/statutory-disclosure/disclosures/monthly-portfolio-disclosure` | Monthly portfolios behind Ant-Design month dropdown | Playwright |
| `union.py` | Union | `unionmf.com/about-us/downloads/monthly-portfolio` | Monthly portfolio PDFs/XLS (8 years) | Playwright `select_option` |
| `taurus.py` | Taurus | `taurusmutualfund.com/monthly-portfolio` | `Monthly_Portfolio_Report` links (3 years × 12 months) | Playwright (Drupal exposed filters) |
| `bandhan.py` | Bandhan | `GET cmsnew.bandhanmutual.com/wp-json/finance-api/v1/posts/disclosures?posts_per_page=2500` | WordPress disclosure posts incl. monthly portfolio files | httpx JSON API |
| `nj.py` | NJ | `downloads.njmutualfund.com/njmf_download.php?nme=127` | Consolidated monthly portfolio XLS/XLSX | httpx+BS4 fast path, Playwright fallback |
| `hdfc.py` | HDFC | `POST cms.hdfcfund.com/en/hdfc/api/v2/disclosures/monthfortportfolio`; factsheet page `hdfcfund.com/mutual-funds/factsheets` | Per-scheme monthly portfolio XLSX (2024–2026 × 12) + grouped/index factsheets from `__NEXT_DATA__` | **curl_cffi** Chrome impersonation (Akamai blocks plain httpx) |
| `kotak.py` | Kotak Mahindra | `kotak.bank.in/MF_Factsheet/equity.html` + `/debts.html`; per-scheme `scheme-pages/*.html` | Per-scheme HTML factsheet pages (top-10 etc., current month) | httpx+BS4 3-stage crawl; AMC's own captcha site (kotakmf.com / Radware) is avoided |
| `iti.py` | ITI | `POST itiamc.com/jeeth/api/v1/catalog/getPartnerDocumentByType` | Portfolio/holding/statement docs | AES-128-CBC request payload (`KEY=aar6tzij8o1snaar`), httpx |
| `axis.py` | Axis | Token page `axismf.com/downloads/products`; `POST axismf.com/cms/product/factsheet` | Per-scheme factsheet PDFs (2024–2026 × 12) | One Playwright load captures Bearer token; queries via httpx |
| `uti.py` | UTI | `GET utimf.com/api/get_investor_scheme_fund` + `get-scheme-portfolio-disclosure` | Scheme-wise portfolio disclosure files (probe trailing 12 months) | httpx, concurrency 6; slowest adapter (~10 min/run) |
| `whiteoak.py` | WhiteOak | `mf.whiteoakamc.com/resources/downloads/factsheet?month=&year=` | Consolidated monthly factsheet PDF (24 months) | Playwright + JS link extractor |
| `edelweiss.py` | Edelweiss | `api.edelweissmf.com/edelweissmf/api/v1/` (`mf/statutory-menus`, `/single`) | Monthly portfolio + factsheet menu files | AES-encrypted responses (`Salted__`, EVP_BytesToKey); curl_cffi required; downloader special-cases 403 |
| `mahindra.py` | Mahindra Manulife | `GET investorapi.mahindramanulife.com/api/v1/web/preLogin/downloads` | Monthly portfolio XLSX + factsheet PDFs + SSDs | AES-256-CBC payload; httpx |
| `jm.py` | JM Financial | `POST jmmfapi.jmfinancialmf.com/api/GetDownloadNew` | Monthly factsheet PDFs; fortnightly portfolio XLSX | AES-256-CBC payload, latin-1 decoded |
| `wealthcompany.py` | The Wealth Company | `wealthcompanyamc.in/literature-forms/portfolio-documents/monthly/` + `scheme-documents/factsheets/` | Monthly/fortnightly portfolio XLSX + factsheet PDFs | httpx + regex on escaped Next.js JSON |
| `sundaram.py` | Sundaram | `sundarammutual.com/Monthly-Fortnightly-Adhoc-Portfolios`; ASP.NET PageMethod `*.ashx?_method=GetCategory` | Monthly portfolio XLSX (equity&FoF, fixed income per month) | httpx POST form `Catid=` |

### Infrastructure adapters (not AMC-specific)

| Module | Site type | Method |
|---|---|---|
| `generic.py` | Server-rendered AMC sites | httpx GET portfolio+factsheet pages, collect `<a href>` ending `.pdf/.xlsx/.xls/.csv/.zip`, lxml beautify |
| `playwright_adapter.py` | JS-heavy sites | Headless Chromium, 3 s wait, JS collects doc links / text "download" |
| `HybridAdapter` | Default for the other 37 AMCs | Generic httpx first; Playwright only if no dated links found |
| `base.py` | Contract | `discover_documents()` filters `discover_documents_all()` by detected month/year; `extract_month_year()` from link text/URL; extensions constant |

37 AMCs use HybridAdapter, e.g. ABSL, SBI, Nippon, Tata, Mirae, Franklin, LIC,
PPFAS, Motilal, Canara, HSBC, Baroda, DSP, Groww, Invesco.

---

## 4. Download layer — `src/pdf_downloader.py`

- `DocumentDownloader` writes to `data/raw/pdfs/<AMC>/<YYYY>/<MM>/` (AMC name:
  spaces→`_`, `/`→`-`), original URL basename, month self-detected from the
  filename/content when possible.
- Plain httpx download with retries; special cases: Edelweiss 403 → curl_cffi;
  HDFC/WAF hosts similarly.
- Link manifest with provenance: `data/logs/portfolio_links/<YYYY-MM>.json`
  (`{filename, url, disclosure_month, disclosure_year, document_type}`).

---

## 5. Parse layer (document → holdings JSON)

Routing by extension (`main.parse_document`, `main.py:93-114`):
`.pdf → parse_pdf`, `.xlsx/.xls → parse_excel`, `.zip → parse_zip`,
`.csv → pandas`, `.html → parse_html`.

### 5.1 PDF agent network (`src/pdf_parser.py` → `src/pdf_agents.py`)

1. Split: PyMuPDF reading-order text → scheme-boundary detection
   (dictionary-first via `src/amfi_nav.get_nav()`, ABSL product labels, legacy
   regexes, LIC TOC ranges) → one sub-PDF per scheme, or page chunks
   (`parser.chunk_pages`, default 6).
2. Parse workers in a `ProcessPoolExecutor` with **pdfplumber**; three
   extractors: regex holdings, pdfplumber tables (heuristic % column),
   one-line-per-holding layouts.
3. Merge grouped → `schemes{}`, flat → equity/debt/sector/top_holdings buckets.
4. Fallbacks: legacy pdfplumber page loop → `src/pdf_segregator.parse_grouped_pdf()`
   → PyMuPDF text.
5. OCR tier (Tesseract via `pytesseract`, DPI 200): plain OCR + geometry OCR
   (word boxes → visual columns) for vector-outline/scanned PDFs.
6. AI tier (optional, `src/ai_extract.py`): OpenRouter vision model on rendered
   pages (≤4 pages @150 dpi) when OCR yields <12 rows; disabled by default;
   cached by source sha256.

### 5.2 Sibling parsers

| Parser | Input | Notes |
|---|---|---|
| `src/excel_parser.py` | XLS/XLSX | SEBI column alias map, per-sheet schemes, header auto-detect, derivative-disclosure block scanner (`derivatives_pct_nav`) |
| `src/html_parser.py` | Kotak-style HTML factsheet pages | Holdings + sector tables |
| `src/zip_parser.py` | ZIP bundles | Extracts per-scheme members, dispatches to pdf/excel/html/csv parsers |
| `src/pdf_segregator.py` | Grouped factsheet PDFs | Standalone splitter used by legacy path |

Parse cache: output JSON must carry `metadata.source_sha256` matching the source
file hash or it is re-parsed. Parallelism: per-document pool in
`src/batch_parser.py` (workers = min(cpu, 8)).

### 5.3 Output layout

| Path | Content |
|---|---|
| `data/raw/pdfs/<AMC>/<YYYY>/<MM>/` | Raw PDF/XLS/XLSX/CSV/ZIP/HTML |
| `data/parsed/amc_websites/<AMC>/<YYYY>/<MM>/<doc_stem>.{json,csv}` | Parsed per-document output |
| `data/parsed/amc_websites/report_<YYYY>_<MM>.json` | Consolidated per-AMC status (documents, schemes, as_of, fallback flag) |
| `data/logs/portfolio_links/<YYYY-MM>.json` | Link manifest / re-download provenance |
| `data/raw/manual_ingest/` | Manual drop folder (`python main.py ingest`) |

---

## 6. Per-scheme metadata miners (local corpus, no scraping)

These read the parsed corpus above (plus factsheet `raw_text`) and write
reference JSONs consumed by the webapp Scheme Details drawer:

| Module | Extracts | Output | Command |
|---|---|---|---|
| `src/scheme_attributes.py` | Shared corpus walk, fund-name merge, SEBI risk-label normalisation | library | — |
| `src/scheme_riskometer.py` | Scheme + benchmark risk on SEBI 6-level scale | `data/reference/scheme_riskometer.json` | `python -m src.scheme_riskometer` |
| `src/scheme_descriptions.py` | Verbatim investment objective + display form | `data/reference/scheme_descriptions.json` | `python -m src.scheme_descriptions` |
| `src/fund_managers.py` | Current fund managers (attribution ladder; never guesses) | `data/reference/fund_managers.json` + `fund_manager_review.csv` | `python -m src.fund_managers` |
| `src/asset_class_breakup.py` | Equity/Debt/Gold/Cash/International/F&O/REIT split | `data/asset_breakup.json` | `python -m src.asset_class_breakup --source all --json-out ...` |

Riskometer/factsheet docs flow through the normal monthly download pipeline;
`main._parse_single_doc` routes filenames containing "riskometer" to the
riskometer parser.

---

## 7. Anti-bot techniques in use

| Technique | Used by |
|---|---|
| `curl_cffi` Chrome impersonation | HDFC discovery/download, Edelweiss, NSE historical API |
| AES payload/response decrypt | ITI (128-CBC), JM (256-CBC), Mahindra (256-CBC), Edelweiss (EVP_BytesToKey) |
| Playwright headless Chromium | Navi, Jio, Union, Taurus, WhiteOak, Axis token capture, NJ fallback, Hybrid fallback |
| Bearer token captured from page | Axis |
| RSC/Next.js payload regex | AMFI portal, HDFC factsheets, WealthCompany |
| Captcha solving (`src/captcha_solver.py`) | **Implemented but unwired** — Kotak sidesteps to kotak.bank.in instead |

---

## 8. Commands

| Command | Purpose |
|---|---|
| `python main.py run [-y][-m][-a AMC][-w workers][--force]` | Full registry fetch + download + parse pipeline |
| `python main.py parse-batch [-a][-y][-m][-w][--force]` | Re-parse already-downloaded docs (sha256 cache aware) |
| `python main.py portfolio-links [-y][-m][-a]` | Adapter discovery → link manifest |
| `python main.py portfolio-download [-y][-m][-a]` | Manifest → download into `data/raw/pdfs` |
| `python main.py ingest` | Manual drop-folder ingest + backfill of unparsed PDFs |
| `python main.py parse <file>` | Parse one file, print JSON |
| `python main.py report [-y][-m]` | Rebuild consolidated report from disk |
| `python main.py list-amcs` | Registry listing with portfolio URLs |
| `python main.py ter`, `amfi-directory`, ... | See [amfi.md](amfi.md) |

Scheduler: `monthly_holdings_fetch`, days 1–5 at 06:00 IST, success marker
`logs/success_<YYYY>-<MM>.marker` (`src/scheduler.py:53-83,263-289`).
