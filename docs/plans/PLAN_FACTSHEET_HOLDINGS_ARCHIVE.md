# PLAN — Factsheet & Holdings Document Coverage (All AMCs, All Released Months)

Date: 2026-10-03
Status: Execution in progress — 2026-10-03 run: portfolio-links manifests for 2026-07..2026-10 pulled (57 AMCs, 2122 links), portfolio-download backfilled all four months, ingest parsed 854 docs, doc_coverage shows 2122/2122 raw + parsed (0 gaps). scripts/doc_coverage.py + scripts/export_factsheet_index.py added; frontend dashboard now renders latest factsheet/portfolio download links for the last months. T6 (monthly cadence) and T7 (sparse-AMC adapter fixes) still pending.

## 1. Current state audit

- **57 AMCs** tracked in `config/amc_registry.json`; 20 dedicated adapters in
  `src/amc_adapters/`, remainder via `HybridAdapter`.
- Documents land in `data/raw/pdfs/<AMC>/<YYYY>/<MM>/` and `data/raw/amc_downloads/`,
  parsed JSON/CSV in `data/parsed/amc_websites/<AMC>/<YYYY>/<MM>/`.
- Per-AMC document counts (files on disk):
  - Heavy: HSBC 1372, Old Bridge 991, Capitalmind 585, Nippon 257, Zerodha 187,
    HDFC 111, Bajaj Finserv 161, Helios 138, ITI 144, ICICI 141, Tata 88, Shriram 83.
  - Sparse/none: Bandhan 0 PDFs (xlsx only), Choice 4 (no PDFs), Edelweiss 2,
    Quantum 1, WhiteOak 1, Axis 3, IL&FS 3, Trust 3, Groww 6, LIC 9, UTI 75,
    Sundaram 8, Tail AMCs (NJ 5, PPFAS 14, quant 7, SBI 7, Mahindra 6).
- Last download run: 2026-08-31 (per file timestamps). Registry monthly pipeline
  (`python main.py run`, days 1–5) has not yet pulled September 2026.
- Parse pipeline: `python main.py portfolio-links` → `portfolio-download` →
  `batch_parser` → `save_parsed` → `report_YYYY_MM.json`.
- Extraction schemas: holdings `weight_pct`/%NAV, scheme attributes, riskometer,
  asset-class breakup; equity holdings via xlsx/zip/pdf/html parsers.

## 2. Gaps identified

1. **No single "released vs local" manifest** — AMC websites expose historical
   links (Axis/Kotak/JM/Union/WhiteOak: 2024–2026), but local corpus is partial;
   no diff between offered and downloaded.
2. **Stale corpus** — last ingest Aug-31; Sep-2026 factsheets/portfolios missing.
3. **Parsed-vs-raw mismatch unknown** — many raw PDFs may lack parsed JSON
   (parse-cache exists but no coverage report for holdings PDFs).
4. **Zero-coverage AMCs** — Bandhan, Choice, Jio, Navi, NJ, PGIM, Union (PDFs
   exist but few/zero), IL&FS, Quantum.
5. **Factsheet vs portfolio conflation** — some AMCs (Groww, Axis few files) only
   have grouped factsheets (partial top-holdings), which are excluded from
   holdings loading; need explicit classification.
6. **No dedupe/version check** — sha256 parse-cache exists, but no re-download
   guard for corrected AMC re-issues.

## 3. Targets

- **T1. Inventory**: per-AMC manifest of every downloadable document link
  (factsheet, monthly portfolio, fortnightly, xlsx/pdf/zip) for every month
  offered on the AMC site; dump to `data/logs/portfolio_links/<YYYY-MM>_full.json`.
- **T2. Gap report**: diff AMC-offered links vs local files →
  `data/reports/doc_coverage_<YYYY-MM-DD>.json`.
- **T3. Backfill**: download all missing months/schemes so every released
  document through the current date (2026-10-03) is present under
  `data/raw/pdfs/<AMC>/<YYYY>/<MM>/`.
- **T4. Parse coverage**: parse-batch all raw holdings docs; target ≥ 95%
  parsed (JSON+CSV), unparsable flagged with reason codes.
- **T5. Extraction targets**: every parsed scheme-month carries holdings with
  %NAV, scheme attributes (category, AUM, launch date, expense, benchmark),
  riskometer, asset-class breakup; derived into `data/parsed/` + webapp db.
- **T6. Cadence fix**: monthly pipeline (days 1–5, 06:00 IST, retry until
  success marker) resumed so Sep-2026 and onward are captured within 5 days of
  release; re-issue detection via sha256.
- **T7. AMCs with zero/sparse coverage**: verify adapter discovery for Bandhan,
  Choice, Edelweiss, IL&FS, Jio, Navi, NJ, PGIM, Quantum, Sundaram, Taurus,
  Union, UTI, WhiteOak; fix Playwright/API failures.

## 4. Execution plan

1. Run `python main.py portfolio-links` across all 57 AMCs (full recency, not
   just last 2 months) → parse manifest.
2. Build gap report script (`scripts/doc_coverage.py`): link-set minus local set.
3. Bulk download missing into `data/raw/pdfs/` (concurrency by AMC, respect
   rate limits, curl_cffi for HDFC/Edelweiss).
4. `src/batch_parser.batch_parse()` over new files; reconcile `parsed` vs `raw`.
5. `src/statement_coverage.py`-style audit for holdings PDFs.
6. Verify September-2026 factsheets present for top-20 AMCs by AUM; spot-check
   extracted holdings vs AMFI portfolio disclosure.
7. Schedule monthly job in `src/scheduler.py`; add alert when success marker
   absent by day 6.

## 5. Success criteria

- 100% of released factsheet/portfolio documents (Aug-2024 → Oct-2026 where
  exposed by AMCs) present locally.
- ≥ 95% of raw documents have parsed JSON/CSV with holdings %NAV.
- `doc_coverage_*.json` shows zero unexplained gaps for top-30 AMCs.
- Monthly pipeline runs unattended; Sep-2026 captured within cycle.
