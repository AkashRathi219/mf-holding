# Factsheet Engine AI — Data Summary & Lovable UI Prompt

## PART A — Summary of all data points currently available (the "webapp" data)

This is the dataset that the UI must be able to query, filter, and visualize.

### A.1 Parsed portfolio holdings (the core dataset)
| Data point | Value |
|------------|-------|
| Total parsed holding rows | **235,343** |
| Holdings with an ISIN | **232,758 (98.9%)** |
| Distinct (AMC, scheme) pairs | **3,101** |
| Distinct AMCs covered | **52** |
| Parse sources | advisorkhoj monthly disclosures + AMC-website monthly portfolios |

Each holding row carries: `scheme_name / fund_name`, `company`, `ISIN`, `quantity`, `market_value`, `percent_nav`, `yield`, `sector/industry`, `section`.

### A.2 Scheme universe & coverage
| Data point | Value |
|------------|-------|
| Universe source | `Combined NAV - 14-Aug-2026.csv` |
| Universe fund-level schemes | 2,096 (2,691 funds / 3,812 fund+plan rows) |
| Schemes covered with holdings | 1,600 |
| Still missing (index/no-disclosure) | 44 (0 active) |
| Index/ETF funds resolvable to Nifty | 31+ resolved, ~68 need external weights |

### A.3 Equity ISIN database (`equity_isin.db` / `.csv`) — 3,941 unique ISINs
| Tag | Count |
|-----|-------|
| **confirmed_equity = 1** (pure listed Nifty stock) | **846** |
| confirmed_equity = 0.5 (mixed: REIT/InvIT/preference/convertible) | 58 |
| confirmed_equity = 0 (non-equity: bonds/CP/ETF/fund) | 3,037 |

**Market-cap tags (for the 846 pure stocks):**
`large` 95 · `mid` 147 · `small` 248 · `microcap` 246 · `ipo` 56 · `sme` 23 · `sectoral` 31

**Sector tags (standard Nifty sectors, 22 distinct):**
Financial Services · Capital Goods · Healthcare · Fast Moving Consumer Goods · Consumer Services · Automobile and Auto Components · Consumer Durables · Chemicals · Information Technology · Services · Construction · Metals & Mining · Power · Oil Gas & Consumable Fuels · Realty · Construction Materials · Textiles · Telecommunication · Media Entertainment & Publication · Utilities · Diversified · Forest Materials

### A.4 Index / benchmark data
- **Nifty constituents** (~205 equity index files): each lists `Company Name, Industry, Symbol, Series, ISIN Code` per index (Nifty 50/100/500, Midcap, Smallcap, Microcap, sectoral, thematic).
- **Debt-index constituents** (`data/nifty/debt_constituents/`): G-Sec / SDL / AAA-Bond / 1D-Rate securities with ISIN + weight.
- Per-index PR/TR time series + manifest/mapping.

### A.5 Reference / status artifacts
`amc_download_links.json` (curated AMC links), `advisorkhoj_catalog.json`, `index_resolved_holdings.json`, `coverage_by_amc`, `no_disclosure.csv` (8), `discovery_needed.csv` (46), `combined_nav`, `navall.txt`.

### A.6 Regulatory / scheme attributes available per scheme
AMC, scheme name, category (Equity/Debt/Hybrid/Solution Oriented/Other), fund-level name, ISIN, sector, market-cap bucket, index/ETF flag, FOF flag, holdings % NAV, yield, maturity (debt).

---

## PART B — Detailed prompt to paste into Lovable (to render the UI)

Copy everything below into Lovable:

---

Build a **B2B Financial Data & Portfolio Analytics web application** called **Factsheet Engine AI** for Indian Wealthtech platforms, Registered Investment Advisors (RIAs), and Mutual Fund Distributors (MFDs). It is a specialized Portfolio Analytics Engine, Portfolio Overlap / Diagnostics tool, and Client Proposal Generator over a large Indian mutual-fund holdings dataset.

### Product identity
- **User:** financial advisors / distributors / RIAs who need fast, objective portfolio diagnostics and client-ready proposal drafts.
- **Tone:** professional, data-dense, dashboards + clean tables. Indian financial notation (₹, lakhs/crores, %). No speculative buy/sell advice; always include a factual/non-advisory disclaimer.

### Core modules (3 top-level areas)

**1. Portfolio Overlap & Diagnostics**
- Input: user picks/enters 2+ schemes (or a scheme list with % weights / ISINs).
- Output:
  - **Portfolio Overlap Matrix** (table) — pairwise stock-level overlap % between schemes.
  - **Concentration Summary** — top 5 underlying duplicate holdings across the portfolio (e.g., total weight in HDFC Bank across 4 funds) and true sector exposure.
  - **Debt Risk Analysis** — for debt schemes show yield-to-maturity (YTM), average maturity, and credit-rating distribution (AAA/AA/A/Unrated).

**2. Proposal & Pitch Deck Generator**
- Output in 3 sections:
  1. **Current Portfolio Diagnostic** — key redundancies, high-overlap pairs, hidden concentration risks.
  2. **Proposed Realignment** — side-by-side comparative table of existing vs recommended.
  3. **Key Rationale** — bullets on yield / turnover / riskometer alignment.
- Auto-append a compliant, non-advisory disclaimer.
- White-label: markdown tables + bold standalone headers for easy copy into branded decks.

**3. B2B API / Data Explorer**
- A data browser over the holdings dataset: search/filter by AMC, scheme, category, ISIN, company, sector, market-cap, index/ETF.
- Mapping helper: NSE/BSE ticker ↔ ISIN ↔ AMFI scheme code ↔ AMC scheme name.
- JSON payload / schema viewer with sample payloads and cURL examples.

### Data model the UI must support (query/filter/visualize)
- **Scheme**: AMC, scheme_name, fund_name, category (Equity/Debt/Hybrid/Solution Oriented/Other), is_index/is_etf/is_fof flags, coverage status.
- **Holding**: company, ISIN, quantity, market_value, percent_nav, yield, sector, section.
- **Security** (from the equity ISIN database): ISIN, name, confirmed_equity (1=pure listed stock / 0.5=mixed REIT·InvIT·preference·convertible / 0=non-equity bond·CP·ETF·fund), **cap** (large/mid/small/microcap/ipo/sme/sectoral), **sector** (22 standard Nifty sectors).
- **Universe**: 3,101 (AMC, scheme) pairs across 52 AMCs; ~235,343 holding rows, ~99% with ISIN; 3,941 unique security ISINs (846 pure listed stocks tagged by cap + sector).

### Suggested screens
1. **Dashboard** — KPI cards: AMCs covered, schemes, holdings, ISIN completeness, pure-stock count; charts for sector & cap distribution; coverage gauge.
2. **Scheme Explorer** — filterable table (AMC/category/cap/sector/search), click a scheme → holdings detail (company, ISIN, %NAV, sector).
3. **Security Directory** — the 3,941-ISIN master table with confirmed_equity / cap / sector filters.
4. **Portfolio Overlap** — scheme picker → overlap matrix + concentration summary + debt risk panel.
5. **Proposal Generator** — pick schemes, choose realignment, render 3-section white-label draft with disclaimer + copy button.
6. **API / Mapping** — JSON payload viewer, ticker↔ISIN↔AMFI mapping search, sample cURL.

### Style
Clean fintech UI: sidebar navigation, cards, sortable/filterable tables, responsive, chart library for distributions, monospaced alignment for numeric columns, Indian ₹ formatting. Light theme with an accent color; professional and dense (data-first), not marketing-heavy.

### Non-functional requirements
- All data queryable client-side (load a provided JSON/CSV dataset or a simple local DB).
- Numeric columns right-aligned and formatted (₹10,00,000, 14.2%).
- A global disclaimer banner: "Diagnostic tool for factual analysis only; not investment advice."
