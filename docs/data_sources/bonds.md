# Bond / Debt-Market Data Sources

Daily NSE debt-market bulk files plus a live JSON snapshot, merged into a
single computed catalog with YTM. `src/bonds.py` is the only fetcher.

---

## 1. Upstream sources

| # | Purpose | URL | Method | Data points |
|---|---|---|---|---|
| 1 | CBM Security Master (~3,700 corporate bonds) | `https://nsearchives.nseindia.com/content/debt/Corporate_bond_report_{DD-Mon-YYYY}.csv` (`src/bonds.py:68`) | GET via `stock_common.http_get`, `Referer: nseindia.com`, timeout 40 | 25 cols: sectype, security, issue name/desc, issuer, face value, credit rating, issue/maturity/record dates, step-up coupons, coupon frequency, next coupon, day-count, floating benchmark, spread, last trade date/price/value, last yield, WA price/yield, traded value, ISIN, status |
| 2 | WDM trading list (G-Secs, SDLs, T-Bills, PSU bonds) | `https://nsearchives.nseindia.com/content/historical/WDM/{YYYY}/{MON}/wdmlist_{DDMMYYYY}.csv` (`src/bonds.py:73`) | same | 13 cols: sectype, security, issue name/desc, issue date, maturity, IP dates, coupon freq, last traded date/price, ISIN, status |
| 3 | CBM daily trades | `https://nsearchives.nseindia.com/archives/debt/cbm/cbm_trd{YYYYMMDD}.csv` (`src/bonds.py:79`) | same | trade date, ISIN, last price, last/WA trade values, last/WA yield |
| 4 | Live debt-market snapshot | `https://www.nseindia.com/api/live-analysis-debt-market` (`src/bonds.py:60`) | cookie-warmed GET, `Referer: .../market-data/live-analysis-debt-market`, Chrome UA | isin, name, coupon, maturity, price, ytm, issuer, rating, segment |
| 5 | Local seeds (gap fill) | `data/nifty/debt_constituents/*.csv` + `zerodha_debt_fund_holdings_31-jul-26.csv` (`zerodha_dirf*` skipped) | file read | ISIN, security name, weight/coupon/maturity where present |

Bulk files exist only on trading days: 404/network miss returns `None` (not an
error). Cached under `data/bond_market/raw/<YYYY-MM-DD>/` as
`corp_master_<date>.csv`, `wdm_list_<date>.csv`, `cbm_trades_<date>.csv`;
existing non-empty files are reused; 0.25 s sleep between downloads.
Live snapshot cached at `data/bond_market/live_debt_market.json`.

ISIN validation: `^IN[A-Z0-9]{10}$`. Sector mapping and maturity inference from
names (`6.48% GOI 06-Oct-2035`, `7.04% GS 2029`) are built in.

---

## 2. YTM computation (`src/bonds.py:292-421`)

Priority (`resolve_ytm`):

1. reported `last_yield`
2. reported weighted-average `wa_yield`
3. computed from coupon + price + maturity (standard price equation, fractional
   period settlement, bisection on yield `[1e-6, 6]`, price sanity `(0, 300]`;
   zero-coupon → money-market annualization)
4. current yield (`coupon / price`)
5. `None`

Each bond records `ytm_source` so the UI can show provenance.

---

## 3. Catalog merge & output

`build_catalog()` uses the newest raw date having any of the three files
(`_latest_raw_set`) and merges in priority order:

```
corporate master (1) → WDM (2) → CBM trades (3) → live snapshot (4) → local seeds (5)
```

Output `data/reference/bonds_catalog.json`:

```json
{
  "as_of": "...", "fetched_at": "...", "sources": [...], "n_bonds": 0,
  "segments": {...},
  "bonds": [{"isin": "...", "name": "...", "coupon": 0, "maturity": "...",
             "price": 0, "ytm": 0, "ytm_source": "...", "days_to_maturity": 0}]
}
```

If no cached raw dumps exist (fresh container), the catalog builds from
live snapshot + seeds only.

Webapp consumer: `webapp/db._bond_catalog` lazy-loads the JSON with R2
`ensure("reference/bonds_catalog.json")`.

---

## 4. Cadence & commands

| Trigger | Detail |
|---|---|
| Scheduler | `daily_bond_refresh`, 21:30 IST daily (`config/settings.yaml:65-70`, `src/scheduler.py:174-193`) |
| Webapp job | `_bond_job` pulls `bond_market` prefix from R2, walks back up to 7 days for a published file, rebuilds catalog |
| CLI | `python main.py bond-refresh`; `python -m src.bonds [--days N] [--build-only] [--live]` (default: 1 successful trading day; walk-back bounded by `days*3+5`) |

Telemetry: pipeline name `bond_refresh`. Health expectation: stale after 48 h
(`webapp/data_health.py:38`).
