# Plan — Pure-NSE Stock Data Pipeline (De-Yahoo + Re-backfill)

Status: **PLANNED — deferred to post-technical-development** · Created: 25-Aug-2026
Type: Stock data cleanliness (long-running backfill) · Priority: after chat v1 stabilization
Execution order (revised 25-Aug-2026): **download-first, fill-last** — all raw data
points land in their folders first; nothing is written into `stock_history/` until
the very last action (AI-driven extraction from the downloaded files into JSON).
Trigger context: stitched-scale corruption found in `stock_history/` during chat
`total_return_price` development (phantom +-50-100% moves: 2020-11-16, 2022-08-10,
2023-08-10, 2025-02-03/07-31, 2026-08-26 — pairs of opposite jumps between segments
sourced at different adjustment scales: NSE bhavcopy raw vs Yahoo split-adjusted).

## Goal

Single-source stock data: **NSE only** (bhavcopy 2020+ / NSE historical API pre-2020 /
NSE corporate actions). Yahoo removed as an active source (code paths gated behind
`STOCK_ALLOW_YAHOO=1`, last-resort for deep dividend history only). Erases the
stitched-scale corruption at the source.

## Phase 0 — Raw download-only pass (FIRST action; folder fill, zero fill-into-JSON)

Collect every raw data point into its folder before any pipeline logic runs.
`stock_history/` is **not touched** in this phase.

- Bhavcopy top-up: `python -m src.stock_price --download-only` — fills
  `data/stock_bhavcopy/` 2020-01-01 -> latest trading day (parallel workers,
  idempotent; corrupt/HTML cached files re-fetched). DONE 25-Aug-2026 through
  25-Aug (1,720 files).
- Pre-2020 depth dump: `python -m src.stock_price --dump-nse-history [--symbols X,Y]`
  — paced 1-year chunks via `nse_session`, raw points saved per symbol to
  `data/raw/nse_historical/<SYMBOL>.json`; resume checkpoint at
  `data/raw/nse_historical/_status.json`. BLOCKED 25-Aug-2026: the historical API
  returns 503 from this network (geo/IP-gated) — rerun from an India-egress
  network/VPN; bhavcopy host is unaffected.
- Corporate-actions dump: `python -m src.stock_actions --dump-nse-actions [--symbols X,Y]`
  — structured rows per symbol to `data/raw/nse_actions/<SYMBOL>.json`
  (+ `_status.json` checkpoint). DONE 25-Aug-2026: 868/868 symbols, 10,595 raw
  rows. Transient Akamai rate-limits handled by cool-down-and-resume; checkpoint
  makes reruns idempotent.
- Empty-window supplement (DONE 25-Aug-2026):
  `--backfill-empty-actions` — 152 symbols whose structured window is empty
  re-probed (still empty on rerun) then pulled from the full-history
  `corporate-announcements` feed: 14 recovered with dividend/split/bonus hits
  (+ PDF attachment URLs); 138 have ZERO matching announcements in NSE (verified:
  e.g. AAVAS = 996 rows, none mention dividends in any field — recent-IPO /
  never-paid cohort).
- Yahoo last resort for the residual 138 (DONE 25-Aug-2026,
  `STOCK_ALLOW_YAHOO=1 --backfill-empty-yahoo`): writes to a SEPARATE folder
  `data/raw/yahoo_actions/` so nse_actions/ stays pure-NSE. Result: 10 with
  events, 128 empty everywhere -> genuinely action-less symbols (recent IPOs,
  never paid). Coverage final: 716 structured + 14 announcements + 10 yahoo =
  740/868 with data; 128 confirmed action-less.
- Financial-results XBRL dump (DONE 26-Aug-2026):
  `python -m src.financial_statements --download-fr-xbrl [--years 5]` — sweeps
  monthly filing-date windows of `/api/corporates-financial-results`
  (`index=equities&period=Quarterly`, NO `fo_sec` filter — that restricts to the
  ~203-symbol F&O list; unfiltered covers ~2,063 symbols/quarter back to 2015+).
  Metadata merges by seqNumber into `data/raw/financial_results_xbrl/_metadata.json`;
  each filing's XBRL XML lands under `<SYMBOL>/<SYMBOL>_<seq>.xml`. RESULT
  (Sep-2021 -> Aug-2026): 17,432 filings kept / 710 of our 868 symbols /
  17,277 XMLs on disk (692 MB) / 155 permanent 404s at source (0.9%, 59 symbols,
  verified dead links). Every row carries audited/unaudited +
  consolidated/standalone flags — the fill/AI-extraction phase prefers Audited.
- Extraction of the downloaded files into JSON (`stock_history/<ISIN>.json`,
  `stock_actions/<ISIN>.json`) happens ONLY in the final phase, done by AI.

## Phase 1 — `src/stock_price.py`: NSE historical backfill (new source)

- Endpoint: `nseindia.com/api/historical/cm/equity?symbol=&series=EQ&from=&to=`
  (DD-MM-YYYY), covers 1994+
- Chunked 1-year windows stitched; reuses `nse_session` from `stock_common.py`
- Pacing ~1.5s/chunk + backoff (403-protection)
- Chain reorder: manual CSV -> bhavcopy (2020+) -> NSE historical (pre-2020) ->
  ~~Yahoo~~ (flag-gated)
- Delete Yahoo-era merge heuristics (`_split_events` scale normalization,
  "Yahoo-era point: already adjusted") — root cause of the corruption

## Phase 2 — `src/stock_actions.py`: corporate actions off Yahoo

Probed live 25-Aug-2026 against the Corporate Filings section
(`nseindia.com/companies-listing/corporate-filings-application`):

- `GET /api/corporates-corporateActions?index=equities&symbol=<SYM>` — **HTTP 200
  from this network** (works with the plain cookie-warmed opener; no curl_cffi
  needed). Rows: `{symbol, isin, comp, subject, exDate, recDate, bcStart/EndDate,
  ndStart/EndDate, faceVal, series}`. Dividend amount / split ratio are inside the
  free-text `subject` ("Dividend - Rs 13 Per Share", "Bonus 1:1", "Face Value Split
  (Sub-Division) - From Rs 10/- To Rs 1/-"). CONSTRAINTS: latest ~20 actions per
  symbol; NO pagination and NO from/to filtering (params accepted but ignored).
  Deep pre-coverage history therefore stays with the preserved Yahoo-era files.
- `GET /api/corporate-announcements?index=equities&symbol=<SYM>` — HTTP 200,
  full history (3,350 rows for TCS), every row carries an `attchmntFile` PDF URL
  on `nsearchives.nseindia.com`; PDF fetch verified (HTTP 200, `%PDF` magic).
  Feeds the dividend-keyword fallback + the download-PDFs-now / AI-extract-last
  workflow for filings.
- `quote-equity` (403) / `corp-info` (404): not usable from this environment.

Plan of record:

- Structured parse of `corporates-corporateActions` rows: extract amount/ex-date
  from `subject`, ratio for bonus/split
- Fallback: structured parse of the integrated `corporate-announcements` feed
  (`_DIV_KEYWORDS`)
- Yahoo events -> env-gated last-resort (deep history only)
- Merge policy: existing `stock_actions/<ISIN>.json` deep history preserved;
  NSE overrides overlapping recent window (fixes the Rs2.5-on-30-Jun-2025
  garbage class). Raw inputs for that merge = phase-0 dumps in
  `data/raw/nse_actions/`.

## Phase 3 — Fill (LAST action — the corruption eraser)

Runs only after Phase 0 downloads are complete. Extraction from the downloaded
raw files into JSON is done by AI as the final step.

- New command: `python -m src.stock_price --rebackfill-nse [--symbols X,Y]`
- Per symbol: wipe `stock_history/<ISIN>.json` -> merge from LOCAL downloads only
  (`data/raw/nse_historical/<SYMBOL>.json` 1994->2019 + `data/stock_bhavcopy/`
  2020->today); NO network calls during fill
- Resume checkpoint: `data/stock_bhavcopy/nse_backfill_status.json`
  (multi-hour run must be resumable)
- Re-upload `stock_history/` to R2 (`deploy/upload_r2.py` pattern) — chatapp
  inherits clean data via R2-only policy

## Phase 4 — Verification checklist

- [ ] 20-symbol sample: continuity audit clean (no unexplained >35% daily moves;
      reuse the chatapp diagnostic)
- [ ] HDFC Bank 1994->2026: continuous through 3 splits, vol ~20-25%
- [ ] Spot-check 20 symbols vs known prices
- [ ] `stock_actions`: recent dividends match NSE announcements; Rs2.5-class
      misattributions gone
- [ ] R2 re-upload complete; chatapp `total_return_price` E2E: realistic vol
      (~20-25%), TR >= raw
- [ ] Yahoo flag default-off verified; no Yahoo calls in logs

## Risks

| Risk | Mitigation |
|---|---|
| NSE historical API geo-blocked (503; revalidated 25-Aug-2026 from this network) | Phase 0 dump is resumable + offline-safe; rerun from India-egress network/VPN. Bhavcopy host unaffected (verified) |
| NSE blocks historical API | `nse_session` cookies + pacing + resumable checkpoints |
| Symbols without EQ-series history (BE/SM, delisted) | Graceful skip + log; Yahoo flag for emergencies |
| Multi-hour run interrupted | Checkpoint file, idempotent re-run |
| Deep dividend history gaps (pre-coverage years) | Existing files preserved; Yahoo last-resort flag |

## Notes

- chatapp needs **no code changes** (R2-only policy; scale-break repair in
  `total_return_adjust` stays as defense-in-depth for residual glitches)
- Post-re-backfill: re-run chatapp `total_return_price` E2E for HDFC Bank and
  refresh the R2-dependent tests
