# Tracker — AMFI Data-Source Expansion (research-information + otherdata connectors)

Status: **IN PROGRESS** · Created: 01-Sep-2026 · Last updated: 01-Sep-2026
Related: `../DATA_SOURCES_RESEARCH.md`, `../DATA_CADENCE.md`, `../SCHEME_DETAILS_STRATEGY.md`,
`PLAN_MONTHLY_REDOWNLOAD_ATTRIBUTES_FNO.md` (mfdata.in retirement), `PLAN_TASK_BACKLOG.md`

> **How to use this tracker** — every actionable step is a checkbox. Tick `- [x]` as work
> completes and append one line per work session to the **Progress log** at the bottom.
> Phase status legend: ⬜ not started · 🔄 in progress · ✅ done · ⚠️ done with gaps (see notes).

| Phase | Scope | Status |
|---|---|---|
| 0 | Crawl + endpoint discovery (probing, param confirmation) | ✅ |
| 1 | Runtime API connectors: scheme-wise disclosure, tracking error/difference, risk parameters | ✅ |
| 2 | Static/reference ingestion: benchmarks list, scheme details, dividends, SSD documents, NFO, m-cap categorisation | 🔄 |
| 3 | AUM pages + universe freshness (fundwise/schemewise AAUM, bifurcation, state-wise, age-wise folio) | ✅ (except direct-plan scheme-level split ⬜) |
| 4 | Scheduler wiring + DATA_CADENCE update | ✅ (data-health entries ⬜) |

## Ask

Crawl `https://www.amfiindia.com/research-information` (and the `/otherdata` hub it links
to), catalogue every link, and assess which sources the webapp should ingest — replacing
the retired mfdata.in tier-1 holdings feed and enriching scheme metadata — then implement
the connectors.

## 1. Crawl findings (01-Sep-2026)

Both hub pages are Next.js apps: visible tiles have no `href`; targets live in the RSC
flight payload (extracted via `\"…\"` unescape). All 20 `/otherdata` items + 44
research-information links resolved.

### 1.1 `/research-information` → sections/links

| Section | Links |
|---|---|
| Quick Access | `/research-information/list-of-group-companies`, CRISIL methodology PDF, `/research-information/commission-disclosure` |
| Financial Data & Scheme Insights | `/research-information/amfi-data`, `/research-information/sub-classification-of-other-scheme`, `/otherdata` |
| AUM Data | `/aum-data/average-aum`, `/aum-data/aum-disclosure`, `/aum-data/age-wise-folio-data`, `/aum-data/classified-average-aum`, `/aum-data/bifurcation-of-aum` |
| Quick-access dropdown | `/net-asset-value`, `/net-asset-value/nav-download`, `/risk-parameters`, `/new-fund-offer`, `/otherdata/fund-performance`, `/otherdata/tracking-error`, `/otherdata/industry-data-analysis`, `/ter-of-mf-schemes`, `/eops`, investor-complaints portal |

### 1.2 `/otherdata` → all 20 items

| # | Item | Target | Webapp value |
|---|---|---|---|
| 1 | Benchmark indices (Tier-1) | `/otherdata/listofbenchmarkindices` | **High** — scheme→benchmark mapping |
| 2 | MF Lite indices (docx) | `uploads/Listof_Domestic_Indicesfor_MF_Lite_Framework_Dec2025….docx` | Medium |
| 3 | AMC/Sponsor investments in schemes | `/otherdata/market-value-of-amc` | Medium — alignment signal |
| 4 | Scheme-wise disclosure (SEBI 25-Aug-22) | `/otherdata/scheme-wise-disclosure` | **Very high** — holdings disclosure rows |
| 5 | Scheme Performances | `/otherdata/fund-performance` | Low — iframe AG-Grid app; we compute returns from NAV history |
| 6 | Tracking error/difference | `/otherdata/tracking-error` | **High** — index/ETF quality metric |
| 7 | Large/Mid/Small-cap stock categorisation | `/otherdata/categorisation-of-stocks` | Medium — official buckets vs NSE-derived |
| 8 | Industry Data Analysis | `/otherdata/industry-data-analysis` | Low — macro dashboards |
| 9 | AMFI Monthly Note | `/otherdata/amfi-monthlynote` | Low |
| 10 | Top-30 cities / PIN map | `/otherdata/list-of-top30` | Low |
| 11 | Debt & MM transactions | `/otherdata/transaction-in-debt` | Low |
| 12 | SAI | `/otherdata/statement-of-Additional-Information` | Low (compliance) |
| 13 | Scheme Details | `/otherdata/scheme-details` | **High** — objective, launch date, category, AUM |
| 14 | Scheme Dividends | `/otherdata/scheme-dividends` | Medium — dividend history |
| 15 | Accounts | `/otherdata/accounts` | Low |
| 16 | Investor complaints | `/otherdata/investor-complaints` | Low |
| 17 | AMC/Trustee directors | `/otherdata/details-of-directors-and-trustees` | Low |
| 18–19 | Whitepapers / vision PDFs | `uploads/…` | Low |
| 20 | Widely tracked & non-bespoke indices (PDF) | `uploads/Listofwidelytrackedandnon_bespokeindices_Mar2026….pdf` | **High** — benchmark identity list |

## 2. Endpoint catalogue (probed live 01-Sep-2026)

Base: `https://www.amfiindia.com`. All GET, JSON unless noted. Page-payload props are in
the RSC flight data of the given page (unescape `\"` → `"` before regexing).
Verified responses noted per endpoint; probing scripts archived in session notes.

| Capability | Endpoint & params | Verified response |
|---|---|---|
| MF directory (57 funds) | page payload of `/otherdata/scheme-wise-disclosure`: `mf_id`,`mf_name`; `quarters[]` with `QuarterName`,`QuarterDate` (ISO, quarter **start**) | ✅ 57 funds |
| MF directory + months (tracking) | page payload of `/otherdata/tracking-error`: `initialMutualFunds[{mfId,mfName}]`, `initialMonthOptions[{MonthYear,Month_Date}]` (Month_Date = `01-Jul-2026`) | ✅ |
| Scheme-wise disclosure (SEBI 25-Aug-22) | `/api/schemewisedisclosure-investment?MF_ID=&strMonth=dd-MMM-yyyy` (quarter start, Title-case; `&excel=true` → xlsx) | ✅ rows `{Scheme_Name,ISIN,Company_Name,Security_Type,MarketValue,MarketValuePercentage}`; `{"message":"Nil"}` = AMC has nothing to disclose; `No data found.` = absent |
| Tracking error | `/api/tracking-error-data?MF_ID=&strdt=dd-mmm-yyyy` (lowercase; month from payload options) | ✅ `{data:[{Scheme_Name,Benchmark,RegularPercent,DirectPercent}]}` |
| Tracking difference | `/api/tracking-difference?MF_ID=&date=DD-MMM-YYYY` (1st of month; error message confirms format) | ✅ `{data:[{Scheme_Name,Benchmark,Y1_R,Y1_D,Y3_*,Y5_*,Y10_*,ReturnLaunch_*}]}` |
| Scheme list per MF | `/api/populate-scheme?MF_ID=` | ✅ `[{scheme_id,scheme_name}]` |
| Scheme details | `/api/scheme-details?MF_ID=&scheme_id=` | ✅ objective, load, type, category, `Launch_Date`, AUM fields |
| Scheme documents (SSD) | `/api/schemes/{scheme_id}/documents` | ✅ `infoDocumentUrl`, `summaryPdfUrl`, `summaryXlsUrl`, `summaryXmlUrl` (portal.amfiindia.com/spages/) |
| Dividend years | `/api/years/scheme-dividend` | ✅ `{years:["All",2026,…]}` |
| Scheme dividends | `/api/scheme-dividend?MF_ID=&strSDid={scheme_id}&strYear=` (`&excel=true`) | ✅ rows `{SD_ID,Nav_name,Div_year,Rate_of_div,Plan,Option}` |
| Risk parameters | `/api/risk-parameter-data-revised?strCatId=17|18&date=dd-MMM-yyyy` (17=large-cap, 18=small-cap; date = `01-Jun-2026`) | ✅ per-scheme stress-test / concentration / volatility |
| New fund offers | `/api/new-fund-offer` (list); `?Scheme_Id=` (detail) | ✅ grouped by MF; detail has objective, launch/closure dates |
| Average AUM fundwise | `/api/average-aum-fundwise` [FYs] → `?fyId=` [periods] → `?fyId=&periodId=` [table] (`&excel=true`) | ✅ per-fund quarterly AAUM incl. FoF split |
| Average AUM schemewise | `/api/average-aum-schemewise?strType=Categorywise\|Typewise&MF_ID=0` [FYs] → `+fyId` [periods] → `+periodId` [table] | ✅ per-scheme quarterly AAUM with `AMFI_Code` (~2.1 MB for all funds) |
| State-wise AUM | `/api/statewise-data?MF_ID=0&date=01-mmm-yyyy` (lowercase ok) | ✅ monthly per-state AUM split |
| Category × ticket-size | `/api/scheme-catwise-data?MF_ID=0&date=` | ✅ T15/T30 table |
| Direct-plan AAUM bifurcation | `/api/bifurcationaumdata?strdt=dd-Mmm-yyyy` (month-end) | ✅ `{TotalAAUMunderDirectPlan, AAUMunderRegisteredAdvisers, AAUMunderPMS, AAUMunderDIYclients}` |
| Age-wise folio / investor classification | `/api/aum-agewise-folio-report?Month=Mon-YYYY` | ✅ investor-class × scheme-type AUM/folios |
| AMC/Sponsor investments | `/api/investmentscheme?MF_ID=&quarterName=dd-mmm-yyyy` (quarter start) | ✅ endpoint live; per-fund data varies |
| AUM disclosure (category/geography) | `/api/get-disclosure-category?fyId=`, `/api/get-disclosureby-geography?fyId=` | ⚠️ live but currently empty payloads |
| Classified AAUM consolidated | `/api/get-classified-average-aum?periodId=&dataId=&mfId=` | ⚠️ 404 for every variant probed — treat as unavailable |
| Fund performance widget | iframe `/polling/amfi/fund-performance` (Angular + AG-Grid, lazy chunk 813) | ⚠️ data API not discoverable without a browser; **low priority** — returns computed from our NAV history |

Already-used AMFI endpoints (unchanged): `DownloadNAVHistoryReport_Po.aspx`,
`NAVAll.txt`, `/api/populate-te-rdata-revised`, `/api/sif-latest-nav`,
`/online-center/portfolio-disclosure` (members directory).

## 3. Mapping — webapp need → current source → AMFI source

| Need | Current source | AMFI source (new) |
|---|---|---|
| Holdings / %NAV | AMC websites (primary), Advisorkhoj (fallback); mfdata.in retired | **scheme-wise disclosure** (per-fund, quarterly; complements monthly AMC portfolios) |
| Index/ETF quality | none | **tracking error + tracking difference** per scheme |
| Risk / stress disclosure | self-computed only | **risk parameters** (SEBI stress metrics, large/small cap) |
| Scheme metadata | factsheet `raw_text` mining | **scheme-details** + **SSD documents** (`/api/schemes/{id}/documents`) |
| Dividend history | none | **scheme-dividend** |
| AUM (fund & scheme level) | dated Combined-NAV snapshot + factsheet text | **average-aum fundwise/schemewise** (quarterly, has AMFI_Code join key) |
| Direct-plan split | none | **bifurcationaumdata** + `/aum-data/bifurcation-of-aum` |
| Investor demographics | none | **age-wise folio report**, **state-wise AUM** |
| Benchmark identity | fragile factsheet parsing | **listofbenchmarkindices** + widely-tracked-indices PDF |
| New-scheme detection | none | **new-fund-offer** API |
| Returns/performance | own NAV-history engine | fund-performance widget — **not needed** (low priority) |

## 4. Implementation

### Phase 0 — Crawl + discovery ✅
- [x] Extract all links from `/research-information` and `/otherdata` (RSC payloads)
- [x] Discover API endpoints from Next.js page chunks
- [x] Probe every endpoint live; confirm param formats + response shapes (table §2)

### Phase 1 — Core connectors (module `src/amfi_otherdata.py`) 🔄
- [x] Shared http client, retries, `Referer` headers (style of `src/amfi_ter.py`)
- [x] Page-payload parsers: `mutual_funds()`, `tracking_month_options()`, `disclosure_quarters()`
- [x] Fetchers: disclosure, tracking error, tracking difference, risk parameters
- [x] Persistence jobs with `src/refresh_log.track` telemetry → `data/raw/amfi_otherdata/` + `data/reference/amfi_*.json`
- [x] CLI `python -m src.amfi_otherdata <job>` + `main.py amfi-otherdata`
- [x] Live verification run (selftest + tracking + risk-params + disclosure)

### Phase 2 — Reference ingestion 🔄
- [x] `scheme-details`, `scheme documents (SSD)`, `scheme-dividend`, `populate-scheme` fetchers
- [x] `new-fund-offer` list + detail fetchers + snapshot
- [ ] Benchmark-identity ingestion: `listofbenchmarkindices` page + widely-tracked PDF → `data/reference/amfi_benchmarks.json` (feeds `index_resolver`)
- [ ] Bulk scheme-details/dividend harvest (57 MFs × schemes) — scheduled job decision
- [ ] M-cap categorisation (`categorisation-of-stocks`) → stock identity enrichment

### Phase 3 — AUM pages 🔄
- [x] `average-aum-fundwise` (FY → periods → table) job
- [x] `average-aum-schemewise` (Categorywise/Typewise, all funds) job — join via `AMFI_Code`
- [x] `bifurcationaumdata` + `statewise-data` + `aum-agewise-folio-report` jobs
- [ ] Direct-plan scheme-level split (via `/aum-data/bifurcation-of-aum` page payload months)
- [ ] Surfacing: webapp scheme card AUM + direct-plan badges

### Phase 4 — Cadence & wiring ✅
- [x] Scheduler job (`monthly_amfi_otherdata`, days 8-12 IST) wired at both
  MonthlyScheduler sites (webapp in-process + workstation CLI) with contract test
- [x] Update `../DATA_CADENCE.md`
- [ ] Data-health screen entries for the new pipelines

## Progress log
- **01-Sep-2026** — Crawl + probe complete (§2 table); `src/amfi_otherdata.py` with all
  Phase 1–3 fetchers/jobs implemented; live verification: mutual-funds (57 funds),
  tracking (691 TE + 698 TD rows, month 01-Jul-2026), disclosure (440 rows for
  01-Apr-2026 quarter: 2 funds with data, 43 Nil, 12 empty), risk-params (33 large +
  36 small, 01-Jul-2026 auto-resolve), aum (FY2026-27 P1: 55 funds AAUM + schemewise
  with AMFI_Code + bifurcation Jul-2026 + 39-state AUM), nfo (9 groups);
  `python -m src.amfi_otherdata selftest` all OK; scheduler job
  `monthly_amfi_otherdata` wired (webapp/main.py + main.py) with
  `otherdata_refresh` settings block; wiring-contract test extended & green;
  ruff clean on new/edited src files.

## 5. Notes / decisions
- `get-classified-average-aum` 404s on every probed variant — excluded (state-wise and
  category tables cover the need).
- `get-disclosure-category/geography` return empty payloads today — endpoints kept in the
  catalogue, not ingested until AMFI publishes data.
- fund-performance widget: the site's own returns viewer; we already compute the same
  metrics from daily NAV history — no ingestion.
- Date-format zoo (probed): disclosure `strMonth` = quarter **start** `dd-MMM-yyyy`
  Title-case; TE `strdt` lowercase `dd-mmm-yyyy`; TD `date` = 1st of month; bifurcation
  `strdt` = month-end; statewise `date` = `01-mmm-yyyy`; risk `date` = `01-MMM-yyyy`;
  age-wise `Month` = `Mon-YYYY`.
