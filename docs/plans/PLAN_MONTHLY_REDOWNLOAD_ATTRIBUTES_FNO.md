# Tracker — Monthly Re-download + Scheme Attributes (TER · Fund Manager) + F&O-aware Asset Classification

Status: **IN PROGRESS** · Created: 31-Aug-2026 · Last updated: 01-Sep-2026
Related: `../SCHEME_DETAILS_STRATEGY.md` (TER/fund-manager display spec), `../DATA_SOURCES_RESEARCH.md`, `../DIRECTION.md` (source priority), `PLAN_TASK_BACKLOG.md`, `../SESSION_REPORT_2026-08-31_09-02_FNO_ATTRIBUTES.md` (detailed session record)

> **How to use this tracker** — every actionable step is a checkbox. Tick `- [x]` as work
> completes and append one line per work session to the **Progress log** at the bottom.
> Phase status legend: ⬜ not started · 🔄 in progress · ✅ done · ⚠️ done with gaps (see notes).

| Phase | Scope | Status |
|---|---|---|
| 0 | Baseline audit + workspace cleanup | ✅ |
| 1 | Re-download Jul-2026 (all AMCs) | ✅ (AMFI tier-1 ⚠️ mfdata excluded per user directive) |
| 2 | TER capture + wiring | ✅ (incl. `ter_as_of` badge + explorer column) |
| 3 | Current fund manager | 🔄 (module + first run ✅ — 152 funds; web-page source + coverage ⬜) |
| 4 | F&O-aware asset classification | ✅ (parser run, classifier re-run, explorer badge; majors reconciled) |
| 5 | Scheme description + Riskometer | 🔄 (modules + attr card ✅; dedicated riskometer PDFs start Sep cycle) |
| 6 | Verification & surfacing | 🔄 (coverage compiled; R2 bundle ⬜) |

## Ask

1. **Re-download** the two document families for every AMC scheme:
   - **Monthly portfolio disclosures** (the "holding statements" — per-scheme portfolio XLSX/PDF/ZIP).
   - **Monthly disclosure documents** (factsheets + the derivative-disclosure sheets AMC sites publish with the portfolio).
2. **Capture TER** (Regular + Direct) per scheme for the disclosure month.
3. **Capture the current fund manager(s)** per scheme — not yet captured anywhere (factsheet `raw_text` has them; nothing extracts them today).
4. **Asset classification where futures & options are a portion** — extend the committed F&O-v1 hedge-sleeve split to all schemes using the `Derivative*` disclosure sheets (sample in repo root: `Monthly HDFC Equity Savings Fund - 31 July 2026.xlsx` — sheets `HDFCMY`, `DerivativeHDFC Equity Savings F`, `DerivativeHDFCMY` with sections *A. Hedging Positions* / B. speculative-arbitrage).
5. **Scheme short description + AMC-assigned Riskometer** per scheme — the factual "Investment Objective / scheme description" text and the monthly scheme + benchmark risk level on SEBI's 6-point scale, as published by the AMC.

## Baseline audit (31-Aug-2026)

| Item | State |
|---|---|
| Registry | `config/amc_registry.json` — 57 AMCs, portfolio + factsheet URLs |
| Jul-2026 AMC-site docs | `report_2026_07.json`: **49/49 processed success, all as-of Jul-2026**; 51 raw dirs under `data/raw/pdfs/` |
| Aug-2026 | `report_2026_08.json`: 1 AMC only (month-end today; Aug disclosures land in early Sep) |
| AMFI tier-1 (`data/parsed/amfi/`) | **empty** — mfdata fetch never populated; retry scheduled |
| TER | `data/reference/ter_07-2026_{schemes,universe,missing}.csv` + `ter_by_isin.json` already generated (`main.py ter`) |
| Fund manager | **no module, no data** — only `ai_extract.py` mentions (and ignores) them |
| F&O classification | `src/asset_class_breakup.py` (fo-v1.0.x) reads `derivative_pct_nav`/`unhedged_pct_nav`, but **no parser emits those fields yet** — the `Derivative*` sheets are not parsed |
| Riskometer / description | **not captured** — `"riskometer"`, `"risk-ometer"` sit in `_IRRELEVANT_PATTERNS` (`main.py:188`), so AMC monthly riskometer PDFs are **dropped before download parsing**; scheme descriptions exist only buried in factsheet `raw_text` |

Target month for this cycle: **Jul-2026** (fully published). Aug-2026 follows automatically via the days-1–5 scheduler job.

## Document taxonomy (what to download)

| Family | Registry key | Content needed | Destination |
|---|---|---|---|
| Monthly portfolio disclosure | `amc_monthly_portfolio_disclosure` | per-scheme holdings, incl. `Derivative*` sheets | `data/raw/pdfs/{AMC}/{YYYY}/{MM}/` → `data/parsed/amc_websites/{AMC}/...` |
| Monthly factsheet | `amc_monthly_mf_factsheets` | TER, AUM, NAV, **fund managers**, returns, benchmark | same |
| AMFI monthly portfolio (tier-1) | — (mfdata.in) | aggregated standardised holdings | `data/parsed/amfi/` |
| AMFI TER export | — (amfiindia.com) | TER Regular/Direct per scheme | `data/raw/ter/` → `data/reference/ter_*.csv` |
| Monthly Riskometer disclosure | AMC disclosure page (bundled with monthly portfolio/factsheet links) | scheme + benchmark risk level per scheme (SEBI 6-level scale) | kept in `data/raw/pdfs/{AMC}/{YYYY}/{MM}/`; parsed by the dedicated riskometer module, **not** the holdings parser |

---

## Phase 0 — Baseline & workspace cleanup ✅

- [x] Audit current download/parse state (report_2026_07/08, registry, ter_*, parsed dirs)
- [x] Identify the F&O sample workbook (`Monthly HDFC Equity Savings Fund - 31 July 2026.xlsx`) and its `Derivative*` sheet layout
- [x] Clean repo root: one-off OCR session + superseded NSE-pull toolkit (≈78 GB `output/`) moved to `archive/scratch-2026-08/{ocr,nse_pull}`; stale root logs deleted; `CAS_sample_*` + plan-sample xlsx kept (referenced by code/plan)

## Phase 1 — Re-download (target Jul-2026, all 57 AMCs) ✅

1. [x] **Refresh registry URLs** — `python main.py amfi-directory` → 57 portal members verified; 51 same, 1 cosmetic diff (Union www prefix), **4 still empty** (Carnelian, Lakshya, Monarch, Nuvama).
2. [x] **Discover + persist links** — `python main.py portfolio-links -y 2026 -m 7` → `data/logs/portfolio_links/2026-07.json`: **827 links across 37/57 AMCs** (744 Jul + 44 Jun + 39 May; recency-window only). ⚠️ UTI adapter probes funds×12 months ≈ 10 min alone — run detached when doing full cycles.
3. [x] **Download** — `python main.py portfolio-download -y 2026 -m 7` → 502 already present + ~230 downloaded, **0 failed**; 20 skipped AMCs = 4 URL-less + big AMCs (HDFC/ICICI/Tata/Axis/Kotak…) whose Jul docs were already on disk via their dedicated adapters.
4. [ ] **AMFI tier-1** — `python main.py amfi-fetch --month 07-2026` → **mfdata.in down (HTTP 522)**; will auto-populate on a later scheduled run. AMC-site remains the working tier.
5. [x] **Parse sweep** — `parse-batch -y 2026 -m 7 --workers 4`: 291 PDFs → 74 parsed / 217 cached / 0 failed; `ingest`: 149 XLSX/ZIP parsed; report rebuilt (50 success / 7 no_documents).
6. [ ] **Backlog / no-disclosure** — reconcile via `src/reconcile_missing.py` + `data/reference/discovery_needed.csv` (32 items: 9 debt-index weights, 8 no-disclosure, 7 plan variants, 4 commodity, 3 BSE, 1 MSCI/Nasdaq). Manual docs for the 2 active backlog funds → `data/raw/manual_ingest/{AMC}/` + `python main.py ingest`.
7. [x] **Past-data purge (user directive)** — only May-2026+ retained: year dirs 2024/2025 + 2026-01..04 deleted (raw + parsed mirrors, 459 MB); second sweep removed **837 files** with pre-May-2026 dates hidden in filenames (unseparated `ddmmyyyy`/`ddmmyy` — Samco overnight 2022-25, Nippon 2013-22, misc); corrupt `Final-Factsheet-170118.pdf` deleted. Final: **396 raw docs, all May-2026+ or undated.**

Exit check ✅: `report_2026_08.json` (rebuilt via `main.py report`) covers 57 AMCs — 50 with documents, 7 no_documents (URL-less/quiet sites).

## Phase 2 — TER capture (mostly built; finish the wiring) ✅

1. [x] `python main.py ter --month 07-2026 --year 2026-2027` (re-run after re-download) → **3,767/3,812 rows matched (98.8%), 2,106/2,137 funds**; 45 missing all reasoned (16 matured Apr-2026, 14 not in export, 12 no disclosure, 2 wound-up, 1 discontinued). Files refreshed: `ter_07-2026_{schemes,universe,missing}.csv`.
   - Fixed on the way: `load_universe()` / `map_universe_to_ter()` in `src/amfi_ter.py` crashed with `NameError: pd` — lazy pandas import added ([slim-deps] regression).
2. [x] **Wire TER into the scheme record** in `webapp/db.py` — was already TER-v2-wired (id-first resolution via `ter_by_isin.json`); 01-Sep added `schemes.ter_as_of` (export date) + fingerprint salt bump, explorer "TER (R/D)" column, Scheme Details KPI as-of badge ("AMFI export Jul-2026").
3. [x] Acceptance: TER visible for every mapped universe fund — **3,516/3,564 schemes carry headline TER (98.7%), 3,270 Regular / 3,223 Direct, as-of stamped**; `meta.ter_misses` lists the residual.

## Phase 3 — Current fund manager (new module) 🔄

Sources, in precedence order (latest wins; keep all with `as_of` + `source` provenance):

1. **AMC monthly portfolio XLSX/PDF headers** — many AMCs print "Fund Manager(s)" under the scheme title (HDFC sample has it on the portfolio sheet).
2. **Factsheet `raw_text`** — regex around "Fund Manager"/"Managed by" per scheme block; multi-manager lists preserved verbatim, then normalised.
3. **AMC fund-manager web pages** — only where the adapter already parses the page; do not build 57 bespoke scrapers.
4. **AI assist** — extend `src/ai_extract.py` (currently skips fund managers) only as fallback for the residual set.

Build `src/fund_managers.py`:

- [x] Input: `data/parsed/amc_websites/**` (+ `data/parsed/advisorkhoj` metadata where name-bearing) — shared foundation `src/scheme_attributes.py` (corpus walk, canon merge, provenance, history).
- [x] Output: `data/reference/fund_managers.json` keyed by fund-level `canon_name` → `{managers[], as_of, source, scheme_variants[]}`; plan-level merge per `strip_plan`. **02-Sep run (attribution ladder v2): 753 funds / 1,233 rows.**
- [x] Validation: plausible person-name gate (token count, keyword-junk rejection); unattributed hits parked in `data/reference/fund_manager_review.csv` (274 rows) — 0 fabricated.
- [x] Wire into Scheme Details ("Managed by …, as of Jul-2026") via `WebDB.scheme_attributes()` + `attributes.fund_managers` in the detail API.
- [ ] Acceptance: ≥90% of funds with ≥1 manager — **at ~21% the ceiling is structural**: Nippon/Bandhan/Kotak/UTI/DSP/Edelweiss corpora carry almost no FM-bearing docs (their factsheets don't print managers and their AMC FM pages were never downloaded; only ICICI's adapter pulls `fund-manager-detail.php` → 112 ICICI funds attributed). Next workstream: add FM-page fetches to the big-AMC adapters (Nippon, Bandhan, Kotak, UTI, HDFC, DSP, Mirae, SBI, ABSL), then re-run; AI assist as the last fallback.
- [x] **Source-3 probe matrix (02-Sep, live)** — the big-gap AMC sites actively resist or simply don't publish scheme→manager maps:
  - **Kotak** ⛔ bot-wall CAPTCHA on every FM route (needs CapSolver budget per `EXECUTION_TRACKER` S5 — human decision).
  - **Nippon** ⛔ Cloudflare 522 (origin down at probe time; plain retry later).
  - **UTI** ⚠️ 14 manager profile pages render (Playwright) but "Funds Managed" = *"No Schemes present"*; sitemap has 0 fund pages; `/api/page/fund-managers` etc. empty.
  - **Bandhan** ⚠️ site is JS-only (`/fund-managers` renders nav shell); WP REST API 401-auth.
  - **DSP** ⚠️ Next.js shell; sitemap has no FM routes.
  - **ICICI** ✅ already harvested via digital-factsheet pages (no separate FM page needed).
  → Conclusion: per-site scraping has a poor effort/yield ratio (exactly the plan's "no 57 bespoke scrapers" warning). **Recommended path to ≥90%: source 4 (AI assist via `src/ai_extract.py`) over the residual set**, plus opportunistic Nippon retry.

## Phase 4 — F&O-aware asset classification (extend fo-v1.0.x) ✅

1. [x] **Parse-side** — `src/excel_parser.py` Derivative-sheet support was already built (adaptive block scanner per SEBI CIR/IMD/DF/11/2010; binds `Derivative*` sheets via sheet-code suffix / sheet codes / content-name match; emits scheme-level `derivatives_pct_nav` {reported, computed} + `derivatives_summary`). Forced re-parse sweep (01-Sep, 3,230 docs, 0 failed) regenerated the corpus with it.
2. [x] **Classifier** — `src/asset_class_breakup.py` consumes per-holding `derivative_pct_nav` (gross → effective unhedged + hedge sleeve, `future_options` bucket); keyword guard intact with tests.
3. [x] **Run across all sources** → `data/asset_breakup.json`: **3,842 schemes classified; 131 with a Futures-&-Options bucket; 4 with real hedge totals** (up from 1 pre-sweep).
4. [x] **Derivatives-using scheme flag** — `holdings_stats` now counts F&O rows **and** hedged sleeves (`pct_nav_hedged > 0`); explorer amber "F&O" badge; detail API already had `hedge_summary` (hedged/unhedged split).
5. [x] Acceptance (reconciled 01-Sep): corpus holds **23 real Derivative workbooks** (HDFC 20, Bandhan 3) → 25 schemes bound; the 4 major derivative users reconcile sheet Σ% vs F&O bucket within ~2% (Equity Savings 29.68/29.57, Arbitrage 67.24/69.04, Multi-Asset 8.51/6.77, Retirement 18.04/17.35); swaps-only debt books ≈ 0 cash MV → 0 bucket (economically correct); `computed` stays signed to match the sheet's own subtotal (test-encoded).

## Phase 5 — Scheme short description + AMC-assigned Riskometer 🔄

1. [x] **Stop dropping riskometer docs** — `"riskometer"` / `"risk-ometer"` removed from `_IRRELEVANT_PATTERNS`; `_parse_single_doc` routes them to `src/scheme_riskometer.parse_document` (payload has no `schemes` mapping, so db loaders skip it — holdings stay clean). Takes effect from the Sep cycle.
2. [x] **Riskometer extraction** — `src/scheme_riskometer.py`: table-row + explicit-pair extraction; **whitelist normalisation against the 6 SEBI levels** (label variants collapsed; anything outside the scale is parked for review, never guessed). Output `data/reference/scheme_riskometer.json` (canon-keyed, `riskometer_history` retained). **First run: 41 funds / 74 rows** — dedicated riskometer PDFs only start downloading with the Sep cycle; factsheet rows are the current (thin) source.
3. [x] **Short description extraction** — `src/scheme_descriptions.py`: verbatim Investment-Objective blocks from factsheet/portfolio `raw_text` (section-break bounded), verbatim text + 2-sentence display form, provenance `{description, display, source, as_of}`. Output `data/reference/scheme_descriptions.json`. **First run: 1,166 funds / 2,900 rows from 1,110 docs (~33% of 3,564 db schemes).**
4. [x] **Matching** — shared `FundMerger` (canon_name fund-level merge, scheme_variants retained); non-plausible names skipped rather than parked (review list plumbed, empty at v1).
5. [x] **Surface** — Scheme Details "Scheme attributes" card: description paragraph, "Managed by …" line, 6-level riskometer gauge (scheme vs benchmark) labelled with as-of month; wired API (`attributes`) + JS + CSS.
6. [ ] Acceptance: riskometer coverage is the open gap (1.2% today — needs the Sep-cycle dedicated PDFs; stale if older than 3 months); description ≥90% needs multi-scheme-workbook attribution; both carry as-of + source already.

## Phase 6 — Verification & surfacing ⬜

- [ ] Coverage report after the cycle: AMCs downloaded / parsed / as-of month; TER matched %; fund-manager coverage %; F&O buckets reconciled vs derivative sheets; **riskometer coverage % (and stale count) + description coverage %**.
- [ ] Surface in webapp: Scheme Details (TER, managers, asset split incl. F&O), explorer columns (TER R/D, manager count, F&O flag).
- [ ] Re-run `deploy/prepare_data.py` so the R2 bundle picks up the new reference files.

## Runbook (ordered)

```powershell
python main.py amfi-directory
python main.py portfolio-links -y 2026 -m 7
python main.py portfolio-download -y 2026 -m 7
python main.py amfi-fetch --month 07-2026
python main.py run -y 2026 -m 7
python main.py ter --month 07-2026 --year 2026-2027
python -m src.asset_class_breakup --source all --json-out data/asset_breakup.json
python -m src.scheme_riskometer --json-out data/reference/scheme_riskometer.json
python -m src.scheme_descriptions --json-out data/reference/scheme_descriptions.json
python -m src.fund_managers --json-out data/reference/fund_managers.json
```

## Risks / edge cases

- **Derivative-sheet variety**: column layouts differ per AMC ("Market value (Rs. in Lakhs)" vs "% to NAV"); parse defensively, skip-not-guess, and record unparsed sheets in the report.
- **65534-row phantom rows** (openpyxl `max_row` lies — seen in `DerivativeHDFCMY`): iterate until blank-run, don't trust `max_row`.
- **Hedge ↔ stock matching**: futures are index-level (Nifty/Bank Nifty) or stock-level; only stock-level hedges attach to a holding row — index hedges go straight to the F&O bucket.
- **AMFI mfdata availability**: tier-1 may stay unreachable; AMC-site remains the working source (existing priority order handles it).
- **Riskometer PDFs are often scanned/image PDFs** — fall back to the existing OCR toolchain (archive/scratch-2026-08/ocr holds the reusable scripts) before marking a scheme unparseable; never infer a level from the scheme category when the disclosure is missing.
- **Riskometer label drift**: AMCs use sub-labels ("Relatively Higher", "(An Open Ended …)" clutter in the row); normalisation must whitelist the 6 levels and park the rest for review.
- **Description block boundaries**: factsheet raw_text interleaves objective text with returns/NAV tables — extract only the objective paragraph (§8 parsing guidance applies).
- **Month-end timing**: Jul-2026 is complete; Aug-2026 cycle starts days 1–5 Sep via scheduler — no manual action needed.

## Progress log

| Date | Phase | Action | Result |
|---|---|---|---|
| 31-Aug-2026 | 0 | Baseline audit; root cleanup (OCR session + NSE-pull toolkit → `archive/scratch-2026-08/`, stale logs deleted) | Root clean; plan created |
| 31-Aug-2026 | 0→1 | Tracker created; execution started | Phase 1 begins |
| 31-Aug-2026 | 1 | `amfi-directory` | 57 verified; 4 URL-less AMCs unchanged |
| 31-Aug-2026 | 1 | `portfolio-links -y 2026 -m 7` (detached; UTI adapter is slow) | 827 links / 37 AMCs, May–Jul-2026 window only |
| 31-Aug-2026 | 1 | Past-data purge: pre-May-2026 dirs + filename-date sweeps (incl. unseparated dates) | 837+ stale docs removed (raw+parsed); 396 current docs kept |
| 31-Aug-2026 | 1 | `portfolio-download` → `parse-batch` (74/217/0) → `ingest` (149 parsed) | Jul-2026 cycle complete; report rebuilt 50✅/7⬜ |
| 31-Aug-2026 | 1 | `amfi-fetch --month 07-2026` | mfdata.in HTTP 522 (provider down) — retry later |
| 31-Aug-2026 | 2 | `ter --month 07-2026` (+ fixed lazy-pandas NameError in `amfi_ter.py`) | 98.8% rows matched; missing all reasoned |
| 31-Aug-2026 | 4 | `asset_class_breakup --source all` | 2,982 schemes; 131 with F&O bucket → `data/asset_breakup.json` |
| 31-Aug-2026 | next | Up next: Phase 3 fund-manager module; Phase 4 Derivative*-sheet parser; Phase 5 riskometer/description modules; Phase 2 webapp TER wiring | — |
| 01-Sep-2026 | 1 | Jul-2026 recovery re-run (3 detached segments; SAMCO junk skipped + 1,428 pre-May-2026 transaction-report files purged) | 53/53 processed AMCs success, all as-of 2026-07; report 53✅/4⬜ URL-less |
| 01-Sep-2026 | 1 | HSBC parse-pool crash (976 jobs @0.0s after a 49.9s failure) diagnosed | Fixed by forced sweep backfill: 1,372/1,372 HSBC docs parsed |
| 01-Sep-2026 | 4 | Forced re-parse sweep (`ingest --force` + targeted office-doc driver `scripts/sweep_force_office.py`) | 3,230 docs regenerated / 1,858 cached / **0 failed** (72 min) |
| 01-Sep-2026 | 4 | Corpus probe: only **23 real Derivative workbooks** (HDFC 20, Bandhan 3) | 25 schemes bound; 6 with per-holding hedge rows; swaps-only books ≈0 (correct) |
| 01-Sep-2026 | 2 | `ter_as_of` column + explorer "TER (R/D)" + "AMFI export {month}" badge; db rebuilt | 3,516/3,564 schemes TER-stamped (98.7%) |
| 01-Sep-2026 | 3 | `src/fund_managers.py` + shared `src/scheme_attributes.py`; first run | 152 funds / 229 rows (attribution bottleneck noted) |
| 01-Sep-2026 | 5 | `src/scheme_riskometer.py` + `src/scheme_descriptions.py`; riskometer docs no longer dropped (routed to dedicated parser) | Riskometer 41 funds; descriptions 1,166 funds; Scheme Details attr card live |
| 01-Sep-2026 | 4 | `asset_class_breakup --source all` re-run after sweep | 3,842 schemes; 131 F&O buckets; 4 real hedge totals; majors reconciled ±2% |
| 01-Sep-2026 | 6 | Full test suite (452 ✅); CAS fixtures relocated to `data/` (seed/movement tests fixed); webapp F&O badge covers hedged sleeves | Coverage: TER 98.7% · descriptions ~33% · managers ~4% · riskometer ~1% (dedicated PDFs start Sep cycle) |
| 02-Sep-2026 | 3 | Attribution ladder v2: 2500-char window, trailing plan-qualifier stripping, name-label patterns, single-scheme/filename hints, review CSV | 753 funds / 1,233 rows (was 152); 274 unattributed → `fund_manager_review.csv` |
| 02-Sep-2026 | 3 | Per-AMC ceiling audit: ICICI 112✅ (adapter pulls FM pages), HSBC 87✅, Motilal 85✅ — but Nippon/Bandhan/Kotak/UTI/DSP/Edelweiss corpora carry ~0 FM docs | Structural: FM-page fetches needed in big-AMC adapters before ≥90% is reachable |
