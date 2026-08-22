# App Review — Action Points (22-Aug-2026)

Full expert review of data aggregation, calculations, and displays.
Items are ordered by priority; each has the file/line reference and the fix.

## P0 — Broken flows (fix immediately)

| # | Issue | Where | Fix |
|---|---|---|---|
| 1 | CAS **JSON upload crashes**: `json.loads` used without `import json` → every `.json` client-document POST returns 500 | `webapp/tools_api.py:282` | add `import json` |
| 2 | **Client create/edit crashes**: `mvClientSave()` reads `#mvClientNotes` which the form no longer renders → TypeError on Save | `webapp/static/js/app.js:1728,1750` vs form at `1693-1697` | re-add the Notes input or drop the reference |
| 3 | **Sector donut inflates "Others"**: equity-only sector weights passed against whole-portfolio denominator; residual dumps debt/gold/cash into last slice ("Others ≈ 27%" while table shows ~2%) | `webapp/static/js/app.js:1203` | use `pfNormalize100(r.sector_split)` like the sibling pies |

## P1 — Scoring / methodology correctness

| # | Issue | Where | Fix |
|---|---|---|---|
| 4 | **Fake 100% compliance** when zero rules evaluate (total=0 → score 100.0, rendered green) | `webapp/strategy_rules.py:221`; colors at `app.js:2168,1488` | return N/A state; UI badge grey |
| 5 | **Unknown treated as zero**: missing asset key defaults to 0.0 → partially-resolved portfolios spuriously breach `min X% debt/international` | `webapp/strategy_rules.py:135-137` | default None → N/A like security-level rules |
| 6 | **"Max single holding" ignores debt**: metric scans only `asset_class=="stocks"`, so a large bond/NCD can never breach | `strategy_rules.py:42-45` + `tools_api.py:863-866` | evaluate over all effective holdings |
| 7 | **Cross-user strategy rules**: `get_rules/set_rules` scoped by strategy_id only; request body feeds strategy_id directly | `webapp/userdata.py:201-222`, `tools_api.py:1059` | join strategies table on user_id |
| 8 | **Mixed adjusted/unadjusted closes**: pre-2020 Yahoo (split-adjusted) spliced to raw bhavcopy without factor → step discontinuities; also server-local-tz epoch conversion (±1 day) and blank bhav closes can overwrite good points | `src/stock_price.py:300-311,251,304` | store adjclose uniformly (or apply split factors); UTC-explicit timestamps; skip incoming close<=0/None |
| 9 | **Index resolver fuzzy over-reach**: substring match w/o word boundaries ("Nifty 500"→Nifty 50 constituents); hybrid gsec-leg file omits equity leg silently | `src/index_resolver.py:34-52,61` | word-boundary matching; most-specific-first guard |

## P2 — Display / polish

| # | Issue | Where | Fix |
|---|---|---|---|
| 10 | Overlap matrix cells lack "%" and two screens use different heat thresholds for the same matrix | `app.js:2103` vs `1313` | unify thresholds; append % |
| 11 | Allocation-model view renders cap split twice with disagreeing legends | `app.js:1993` vs `2010` | use pfPieFull100 for both |
| 12 | Donut hover dead-zone upper-right quadrant (~25% of ring); null values silently drop bars | `webapp/static/js/charts.js:140-143,16-32` | normalize pointer angle into drawn space; NaN guard |
| 13 | Minor formatting: overlap `d.ytm ? d.ytm*100 : null` treats legit 0 as missing; bonds-table coupon lacks "%"; dashboard divide-by-zero edge | `app.js:1326,606,115` | null-guards, % suffix |
| 14 | Housekeeping: `feedback.json` written under `webapp/data/` not repo `data/`; deleted strategies leave dangling FKs; corrupt JSON blobs render as empty portfolios; NAV backfill is first-write-wins (AMFI revisions never propagate); `_norm_code` strips leading zeros | `main.py:320`, `userdata.py:190-198,118-133`, `nav_history.py:243,84-92` | path fix; cascade delete; surface parse errors; revision-aware upsert; keep code as string |

## P3 — Financial-practice upgrades (not bugs)

- Flag computed bond YTMs whose last-trade date is older than ~90 days (amber marker) — they are quoted at trade date, not today.
- T-bill/STRIPS yields use effective-annualized convention (~10–30bp off NSE discount-yield quotes) — fine for comparison, label it.
- Add modified duration to debt analysis (inputs already available).
- Overlap keyed by ISIN-or-name can understate debt overlap (same G-Sec, different names) — consider issuer+coupon+maturity key fallback.
- Consider defaulting the Bonds tab to traded-only for advisor use.

---

## Data priority-order & completeness audit (22-Aug-2026)

### Mutual funds

| Check | Result |
|---|---|
| **Priority tier 1 (AMFI) populated?** | ❌ **`data/parsed/amfi/` is EMPTY (0 files)**. `webapp/amfi_fetch.py` exists but is not wired into `run_pipeline` or the scheduler → the "authoritative" tier never runs. Every scheme silently falls to tier 2+. |
| Chosen-source distribution | AMC websites 1,121 schemes · Advisorkhoj 1,063 · index 124 · universe-only 205 (200 no-disclosure + 5 discovery) |
| Priority violations | 0 schemes use a lower-priority source while a higher-priority snapshot exists on disk (tier logic works; there is just nothing in tier 1) |
| Holding quality by source | AMC sites: 71,847 rows, 98.9% ISIN, 98.9% %NAV · Advisorkhoj: 51,272 rows, 98.8% ISIN, 99.4% %NAV · index: 10,064 rows, 100%/100% |
| Snapshot recency | AMC sites uniformly **31-Jul-2026** ✓; Advisorkhoj spans **2021-12-23 → 2026-07-31** — 111 of 2,286 covered schemes are older than Jun-2026 or undated (stale Advisorkhoj snapshots still winning) |
| Universe reconciliation (plan-stripped canonical) | 2,105 distinct funds → **1,675 covered (79.6%)**, 430 uncovered = 161 index/ETF + 60 debt-index-ish + 209 active equity/hybrid. (DIRECTION.md's 97.8% used looser fund-level matching incl. benchmark resolution; the webapp DB strict-match rate is lower.) |

**Actions (new):**
- MF-A1 (HIGH): wire AMFI monthly ingestion into `run_pipeline`/scheduler (`webapp/amfi_fetch.py`) so tier-1 actually populates; re-run DB build.
- MF-A2 (MED): refresh stale Advisorkhoj-only schemes (>180d old) or mark them visibly stale in UI.
- MF-A3 (MED): reconcile the ~209 active funds missing coverage against `reconciled_active_download.csv`; either ingest or tag no_disclosure.
- MF-A4 (LOW): index/ETF canonical-name matching loses some funds vs universe — improve matcher before concluding absence.

### Stocks

| Check | Result |
|---|---|
| Identity | 868/868 confirmed-equity ISINs have NSE symbols ✅ |
| Price history | 868/868 files; median 3,622 points; **867/868 fresh ≤6 days** ✅; pre-2020 depth in 557 files; only 2 files with big recent gaps |
| Corporate actions | 868/868 files ✅ |
| Reports (NSE announcements) | 868/868 files ✅ |
| Manual overrides | 0 present (chain is bhavcopy→Yahoo as designed) |
| Known caveat | adjusted(Yahoo)/raw(bhavcopy) splice — see P1 #8 |

**Action:** STK-A1 (LOW): backfill the 1 non-fresh file and inspect the 2 gap files.

### Other
- BUG (new): `python main.py stock-status --json` crashes — the `--json` flag parameter shadows the `json` module (`main.py` stock_status command). Rename parameter.

