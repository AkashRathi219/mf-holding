# Session Report — Jul-2026 Recovery · Forced Re-parse Sweep · Scheme Attributes (F&O / TER / Managers / Riskometer)

Span: **31-Aug-2026 → 02-Sep-2026** · Companion tracker: `docs/plans/PLAN_MONTHLY_REDOWNLOAD_ATTRIBUTES_FNO.md`
Scope: recovery of the Jul-2026 disclosure cycle, forced re-parse sweep for Derivative-sheet data, Phase 2–6 build-out of the scheme-attributes stack.

---

## 1. Jul-2026 recovery cycle

| Segment | Result |
|---|---|
| Run 1 (10:34→19:49) | 26 AMCs complete; parse pool crashed at HSBC (see §2); died at AMC #28 |
| Run 2 (20:17→23:18, resumed at Jio BlackRock) | Completed through Old Bridge/PGIM/PPFAS/quant/Quantum; **Samco skipped** (3,961 junk links — mostly 2023-24 SEBI transaction reports; 1,428 files deleted, 36 kept) |
| Run 3 (23:20→23:48, resumed at SBI) | 11 success / 0 failed / 1 skipped (Sundaram) / 172 cache-hits |

**Final state:** `report_2026_07.json` → **57 AMCs: 53 success / 4 no_documents** (Carnelian, Lakshya, Monarch, Nuvama — URL-less). All processed AMCs as-of **2026-07**. mfdata.in (`amfi-fetch`) deliberately **excluded** per user directive.

## 2. HSBC parse-pool crash → root cause → fix

- Symptom: 897 HSBC raw docs with no parsed JSON; log shows 976 `-> error` jobs **all within one second** (14:18:15–16), first error a 49.9 s job (`hsbc-kim-elss-tax-saver-fund.pdf`) — worker-pool death, not per-file corruption.
- Fix: covered by the forced sweep's backfill leg — **HSBC now 1,372/1,372 parsed**.

## 3. Forced re-parse sweep (01-Sep)

Two-stage approach after `parse-batch`'s pool proved wedged (0 items, ~0 CPU — workers die at startup in the batch env):

1. `main.py ingest -y 2026 -m 7 --force` (**new `--force` flag added**) — killed early (was duplicating PDF work).
2. `scripts/sweep_force_office.py` (**new**) — sequential driver: force re-parse of all **office docs** (xlsx/xls/zip/xlsb/csv — where `Derivative*` sheets live) + backfill of any doc missing parsed output.

**Result: 3,230 docs re-parsed / 1,858 cache-skipped / 0 failed / 72 min.**

### Derivative-sheet ground truth (probed via `xl/workbook.xml`)

- Only **23 real `Derivative*` workbooks** exist in Jul-2026 (HDFC 20, Bandhan 3) — the small surface is the AMC disclosure reality, not a parser gap. (An earlier probe over-counted 48 by matching `definedName` attributes — corrected to `<sheet>` tags.)
- Parsed corpus: **25 schemes** carry bound `derivatives` blocks; **6** with per-holding `derivative_pct_nav` rows; Bandhan's plain `Derivative` sheet unbound (1 warning, expected).

## 4. Code changes (by phase)

| Phase | File | Change |
|---|---|---|
| 2 | `webapp/db.py` | `schemes.ter_as_of` column + populate from `ter_by_isin.json` record date; fingerprint salt bump (`+ter-as-of`) |
| 2 | `webapp/main.py`, `static/{app.html,js/app.js}` | Explorer "TER (R/D)" column; Scheme Details KPI badge "AMFI export {Jul-2026}" |
| 4 | `webapp/db.py` | `holdings_stats` counts F&O rows **and** hedged sleeves (`pct_nav_hedged > 0`) |
| 4 | `webapp/{main.py,static}` | Amber "F&O" explorer badge (`n_fno`) |
| 3 | `src/fund_managers.py` | **v2 attribution ladder**: 2500-char window → name-label patterns → single-scheme/filename hints; review CSV export; person-name validation (0 fabricated) |
| 3/5 | `src/scheme_attributes.py` | **new shared foundation**: corpus iteration, `FundMerger` (canon merge + history), SEBI level normaliser, `plausible_fund_name`/`plausible_person` |
| 5 | `src/scheme_riskometer.py` | **new**: dedicated riskometer parser (PDF routing + corpus mining), 6-level whitelist, review parking |
| 5 | `src/scheme_descriptions.py` | **new**: verbatim Investment-Objective extraction + 2-sentence display form |
| 5 | `main.py` | `riskometer`/`risk-ometer` removed from `_IRRELEVANT_PATTERNS`; routed to dedicated parser (payload has no `schemes` → db loaders skip) |
| 5 | `webapp/db.py`, `webapp/main.py` | `WebDB.scheme_attributes()` read-time lookup (reference files kept OUT of fingerprint — monthly refresh never forces a rebuild); `attributes` in scheme-detail API |
| 5 | `webapp/static` | "Scheme attributes" card: description paragraph, "Managed by …" line, 6-level riskometer gauge (scheme vs benchmark) |
| — | `webapp/seed_samples.py`, `webapp/main.py`, `tests/test_portfolio_movement.py` | **pre-existing breakage fixed**: CAS fixtures relocated to `data/` (root copies had been deleted in the 31-Aug cleanup; code/tests pointed at root) |

## 5. Data artefacts produced

| Artefact | Content |
|---|---|
| `data/reference/fund_managers.json` | **753 funds / 1,233 rows** (attribution ladder v2); `fund_manager_review.csv` holds **274 unattributed FM hits** |
| `data/reference/scheme_descriptions.json` | **1,240 funds / 3,062 rows** (verbatim objective blocks) |
| `data/reference/scheme_riskometer.json` | **41 funds / 74 rows** (dedicated riskometer PDFs start landing with the Sep cycle) |
| `data/asset_breakup.json` | **3,842 schemes; 131 F&O buckets; 4 real hedge totals** (was 2,982/131/1) |
| `data/webapp.db` | 3,564 schemes; TER **3,516 (98.7%)** with `ter_as_of`; 6,956 `future_options` holding rows |

## 6. F&O acceptance reconciliation (Phase 4)

| Scheme | Sheet Σ% (computed, signed) | F&O bucket | Δ |
|---|---|---|---|
| HDFC Equity Savings | −29.57 | 29.68 | ~0.1 |
| HDFC Arbitrage | −69.04 | 67.24 | ~1.8 |
| HDFC Retirement Savings | −17.35 | 18.04 | ~0.7 |
| HDFC Multi-Asset | −6.77 | 8.51 | ~1.7 |

- `computed` is **signed on purpose** — it matches the sheet's own reported subtotal (test-encoded contract in `test_excel_derivative_classification.py`); magnitude is applied downstream.
- Swaps-only debt books show ≈0 cash MV → 0 F&O bucket (economically correct; notional-only disclosures).

## 7. Source-3 probe matrix (02-Sep, live) — AMC fund-manager pages

| AMC | Outcome |
|---|---|
| ICICI | ✅ harvested via digital-factsheet pages (112 funds) |
| Kotak | ⛔ CAPTCHA bot-wall on all FM routes → needs CapSolver budget (`EXECUTION_TRACKER` S5) |
| Nippon | ⛔ Cloudflare 522 (origin down at probe time — plain retry later) |
| UTI | ⚠️ 14 manager pages render but "Funds Managed" = *"No Schemes present"*; sitemap has 0 fund pages; API slugs empty |
| Bandhan | ⚠️ JS-only site; WP REST API 401 |
| DSP | ⚠️ Next.js shell; no FM routes in sitemap |

**Conclusion:** per-site scraping has poor effort/yield (validates the plan's "no 57 bespoke scrapers" rule). Path to ≥90%: **source 4 (AI assist via `src/ai_extract.py`)** over the residual set + opportunistic Nippon retry.

## 8. Coverage snapshot (vs 3,564 db schemes)

| Attribute | Coverage |
|---|---|
| TER (headline) | **98.7%** |
| Description | ~35% |
| Fund managers | ~21% |
| Riskometer | ~1% (Sep cycle unlocks the primary source) |

## 9. Open items / next actions

1. **Phase 3**: AI-assist residual extraction (`src/ai_extract.py` extension) → ≥90% acceptance; Nippon retry.
2. **Phase 5**: Sep-cycle downloads now capture riskometer PDFs → re-run builder, drive riskometer coverage up; description attribution for multi-scheme workbooks.
3. **Phase 6**: `deploy/prepare_data.py` re-run so the R2 bundle ships the new reference files.
4. **Phase 1 (blocked)**: mfdata.in retry — excluded per standing user directive.
5. Tests: **452/452 passing** at session close.
