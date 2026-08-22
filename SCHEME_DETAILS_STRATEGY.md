# Scheme Details — Data Research & Finalization Strategy

**Scope:** How to produce **sensible, correct, client-ready** results for the *Scheme
Details* view (the drawer opened from the Scheme Explorer) — and the data-cleaning /
enrichment rules every scheme must pass before results are shown.

This document is the **authoritative spec** to follow while finalizing the details
for any selected scheme. It is grounded in a data audit of the parsed dataset
(`data/parsed/**`), the universe file, the securities directory, and the index
reference.

---

## 1. Objective

The Scheme Details view must answer, for any scheme, five questions:

1. **What does it hold, and at what weight?** (the primary outcome — allocation %)
2. **How is it split?** Equity vs Debt vs Cash/Other.
3. **What are the characteristics of each holding?** ISIN, sector, market-cap bucket.
4. **How has it performed?** vs its Tier-I benchmark and the additional benchmark
   (1/3/5/10-yr, since inception, annualised).
5. **Key scheme attributes.** AUM, NAV, TER (Regular & Direct), benchmark, YTM/duration
   (debt), coverage status.

Rules below are ordered by the impact they have on correctness.

---

## 2. Data sources & what each provides

| Source | Path | Provides | Caveats found in audit |
|---|---|---|---|
| **AMC-website monthly portfolio** | `data/parsed/amc_websites/**` | holdings with **`percent_nav` (fraction, 0–1)**, `section` (Equity/Debt), `market_value`, `quantity`, `isin` | scheme name is often a **sheet code** (PPFAS: `PPFCF`, `PPLF`…) → currently discarded as junk; company names sometimes mangled |
| **Advisorkhoj** | `data/parsed/advisorkhoj/*.json` | full scheme name, holdings, `isin` | **`pct_nav` is often absent** (e.g. PPFAS); `value` in lakhs; no `section` |
| **AMC factsheet (raw)** | `data/parsed/amc_websites/**/…factsheet….json` | `raw_text` contains **returns, benchmark, NAV, expense ratio, AUM, fund managers** | unstructured text; `equity_holdings`/`raw_tables` arrays are empty in many files; company names mangled ("Limited Power") |
| **Index-resolved** | `data/reference/index_resolved_holdings.json` | benchmark constituents for index/ETF funds | **no `percent_nav`** → equal-weight fallback |
| **Universe** | `data/universe/Combined NAV - 14-Aug-2026.csv` | NAV, TER (per plan), AUM, category, YTM, duration, avg maturity, plan | plan-level rows; needs matching to fund-level schemes |
| **Securities directory** | `data/reference/equity_isins.csv` / `equity_isin.db` | per-ISIN canonical name, `confirmed_equity`, `cap`, `sector` | source of truth for enriching/cleaning holdings |
| **Coverage files** | `no_disclosure.csv`, `discovery_needed.csv` | coverage status | only for flagging, not for holdings |

---

## 3. Problems found in the audit (why results are currently not sensible)

1. **Allocation (%NAV) is missing for many schemes.**
   - **420 / 2,717** schemes-with-holdings have **no `percent_nav` at all**.
   - **16,539 holdings** have `market_value` but no `percent_nav` (weight is *computable*).
   - Index/ETF holdings (13,014 rows) have no `percent_nav`.
   - Root causes: (a) source-picker prefers the source with *most rows*, not the one
     *with weights*; (b) PPFAS-style sheet-code schemes are dropped, losing the
     weighted AMC-website source.

2. **Equity/Debt split is unreliable.**
   - `section` values are inconsistent: `"Equity & Equity related"`,
     `"EQUITY & EQUITY RELATED"`, `"Debt Instruments"`, `"DEBT INSTRUMENTS"`,
     `"Certificate of Deposit"`, plus 117,435 rows with **empty** section (advisorkhoj).

3. **`"Total"` / `"GRAND TOTAL"` section labels are NOT aggregate rows** — audit shows
   they are real holdings (valid ISINs) with a mislabelled section. **Do not drop by
   section value alone.**

4. **`"TRP"` and other odd tokens** are **parsing artifacts** inside debt-security
   names (e.g. `"TRPIN 7.45 01/19/28"`), not financial metrics.

5. **Company names are sometimes mangled** (`"Limited Power"` for Power Grid,
   `"Computer Software"` for Microsoft, `"This Scheme"` for a scheme name) — never
   render these raw; resolve via ISIN → canonical name.

6. **Returns / benchmark / expense data exists only as unstructured factsheet text** —
   needs an extraction step before it can be surfaced.

---

## 4. Source selection & fund identity resolution (DO FIRST)

Goal: pick, for each fund, the **single best holdings snapshot** and attach the right
attributes.

1. **Resolve sheet codes → real fund names.** Before discarding a scheme as a "code",
   check a **code→fund map** built from the same AMC's *factsheet* (which carries the
   real name + code in one document) and from advisorkhoj. PPFAS example:
   `PPFCF → Parag Parikh Flexi Cap Fund`, `PPLF → Parag Parikh Liquid Fund`,
   `PPCHF → Parag Parikh Conservative Hybrid Fund`, `PPTSF → Parag Parikh Tax Saver`.
   Do **not** discard `PPFCF` as junk if it can be mapped to a real fund.

2. **Prefer the source that has weights.** When the same fund appears in multiple
   sources (advisorkhoj + amc_website + index), select the source snapshot by, in order:
   1. **has `percent_nav`** (weighted) — required for the primary outcome;
   2. highest **ISIN coverage**;
   3. most **clean holding rows**;
   4. most **recent** `as_of`.
   The current "most rows" rule must be replaced with this priority.

3. **Dedupe per security.** Within the chosen snapshot, key holdings by `isin`
   (fallback: canonical company name) and sum `percent_nav` / `market_value`; never
   merge `percent_nav` across **different as-of dates**.

4. **Match universe attributes** (AUM, NAV, TER-regular, TER-direct, category, YTM,
   duration, maturity) onto the fund-level scheme using plan-stripped, brand-normalised
   names (`strip_plan` + `canon_name`), preferring the Regular plan row for fund-level
   stats and retaining both TERs.

---

## 5. Allocation (%NAV) strategy — the PRIMARY outcome

Present a clear, auditable weight for every holding. In priority order:

1. **Use source `percent_nav`** after normalising the scale:
   - fraction (max < 2) → ×100; else treat as percent.
2. **If `percent_nav` is missing but `market_value` is present** (16.5k rows):
   compute `pct = market_value / Σ market_value × 100` for that scheme.
   Label it *"computed from market value"*.
3. **If a scheme has NO weights at all** (420 schemes, incl. index/ETF):
   - **Equity index/ETF** → equal-weight fallback (`100 / n` per holding),
     labelled *"equal weight (benchmark)"*.
   - **Debt** → use instrument weights from the factsheet if available, else equal weight.
4. **Always show the Σ%NAV** in a total row, and **flag divergence from 100%**:
   - `> 100%` → typical for arbitrage/derivatives (leverage) — show as-is with a note.
   - `< 100%` by a margin → show residual as *"Net cash & other assets"* and flag the
     gap.
   - Add a **data-quality badge** when the weight is computed/estimated, not sourced.

**Definition of done (allocation):** every row shows a numeric %; the scheme shows a
sum; computed/estimated weights are explicitly marked.

---

## 6. Portfolio composition: Equity / Debt / Cash/Other

Build a **normalised section classifier** so the split is consistent:

- **Equity:** `equity`, `stock`, `equity & equity related`, `equity and equity
  related`, `preference shares` (mark as preference), `warrants`.
- **Debt:** `debt`, `bond`, `debenture`, `g-sec`, `sdl`, `ncd`, `certificate of
  deposit`, `commercial paper`, `treasury`, `money market`, `floating`.
- **Cash/Other:** `cash`, `net current assets`, `net receivables`, `units of
  international funds`, `mutual fund units`, `reits`, `invits`.
- Normalise **case/whitespace** before matching (`EQUITY & EQUITY RELATED` ≡
  `Equity & Equity related`).
- **Do not drop** rows just because `section` is `"Total"`/`"GRAND TOTAL"`/empty —
  classify them by ISIN + security-type (see §7). Drop only rows that are not real
  holdings (§7 `_looks_like_holding`).

Render three grouped tables: **Equity**, **Debt** (with YTM / maturity / rating from
universe + security tags), **Cash & Other** — each with its own subtotal, and a grand
total row.

---

## 7. Security enrichment & name cleaning

For **every** holding, join by ISIN to the securities directory:

1. **Canonical name** ← `securities.name` (never display the mangled parsed name).
2. **Market-cap bucket** ← `securities.cap` (large/mid/small/microcap/ipo/sme/sectoral).
3. **Sector** ← `securities.sector` (22 Nifty sectors), falling back to the holding's
   own sector only when absent.
4. **Security type** ← `confirmed_equity` (1 = pure listed stock, 0.5 = mixed
   REIT/InvIT/preference, 0 = non-equity bond/CP/ETF/fund).

**If ISIN is missing** (or not found): fuzzy-match the company name to
`securities.name`/`name_aliases` to recover the canonical ISIN + attributes. If still
unresolved, show the raw name but mark it *"unidentified security"* — never hide it.

**Holding validity (`_looks_like_holding`):** keep a row only if it has a valid ISIN
**or** a numeric `%NAV` / `market_value` / `quantity`. Header/footnote text takes
precedence and is always dropped (e.g. `Top Ten Holdings`, `# Non Sensex Scrips`,
`~ YTC …`, `@ Less than 0.01%`, `TRPIN …`). A stray number on a footnote row must not
save it.

---

## 8. Returns, benchmark & TRP

Extract from the factsheet `raw_text` (the only place this lives today), per scheme:

1. **Periodic returns table** for `1Y / 3Y / 5Y / 10Y / Since-Inception`,
   **annualised**, for columns:
   - Scheme **Regular**, Scheme **Direct**,
   - Tier-I **benchmark** (e.g. PPFAS = Nifty 500 TRI),
   - **additional benchmark** (Nifty 50 TRI).
2. **TER / Base Expense Ratio** for Regular & Direct (e.g. PPFAS Regular 1.04%,
   Direct 0.52%) — cross-check with universe TER and prefer the more specific source.
3. **NAV** for Regular/Direct (Growth & IDCW), **AUM**, **date of allotment**,
   **fund managers** (optional, metadata only).

**Parsing guidance:** the raw text interleaves the returns matrix with NAV and expense
figures — parse by locating the benchmark columns (`Vs Benchmark Indices`) and the
period markers, and **validate** that each period maps to exactly one number per
column; drop any period with missing/`—` values rather than guessing.

**Display:** a compact performance table (rows = periods, columns = Regular/Direct/
benchmark/additional benchmark), with `—` for unavailable, plus a one-line note that
past performance is not indicative of future returns.

**"TRP":** confirmed to be a **parsing artifact** in debt-security names — do not
surface it as a metric; it is handled by §7 name cleaning.

---

## 9. Rendering & checksum rules

- Numeric columns **right-aligned, monospaced**, Indian format (`₹`, lakhs/crores).
- `%NAV` → two decimals; YTM/TER → three decimals (as %); AUM → `₹X cr`.
- Every holdings table ends with a **subtotal + grand total row**.
- Show **source** and **as-of date** of the chosen snapshot, and a
  **weight-origin badge** (sourced / computed / estimated).
- Keep the global disclaimer visible.

---

## 10. Edge cases (must not regress)

| Case | Rule |
|---|---|
| Arbitrage / leverage → Σ%NAV > 100% | show real sum, add "includes derivative exposure" note |
| Multiple plan rows for one fund | collapse to fund level; keep Regular/Direct TER separately |
| Index / ETF fund (no weights) | equal-weight fallback, labelled |
| Debt fund | add YTM / duration / avg-maturity panel from universe |
| Scheme has no holdings (`no_disclosure`) | show status + reason, no fake table |
| Same security under two names | unify by ISIN via securities directory |
| Mangled names (`Limited Power`, `This Scheme`) | replace with canonical name |
| `Total`/`GRAND TOTAL` section label | treat as real holding; classify via ISIN |

---

## 11. Acceptance criteria (Definition of Done)

A scheme detail view is **finalised** only when ALL hold:

- [ ] **Every holding shows a weight (%)** — sourced, computed, or clearly-marked estimated.
- [ ] **Σ%NAV total row** shown; deviation from 100% is explained/flagged.
- [ ] **Equity / Debt / Cash** grouped with subtotals and consistent section labels.
- [ ] **No footnote/header/garbage rows** (`Top Ten`, `Non Sensex`, `TRPIN`, `YTC`…).
- [ ] Every holding has canonical **name, ISIN, cap, sector** (via securities directory).
- [ ] **TER Regular & Direct**, **AUM**, **NAV**, benchmark shown when available.
- [ ] **Returns** (1/3/5/10Y + SI, annualised) vs benchmark shown when available.
- [ ] Source + as-of + weight-origin badge present.
- [ ] Global disclaimer present.

---

## 12. Non-advisory disclaimer

Every rendered result carries:

> "Diagnostic tool for factual analysis only; not investment advice. Past performance
> is not indicative of future returns."
