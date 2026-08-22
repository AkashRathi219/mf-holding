# Model Portfolios

Model portfolios are **strategic asset-allocation (SAA) plans** — generic targets, not
lists of specific funds/stocks. Advisors create them once and deploy them to many clients.

## What a model portfolio contains

Stored per model as an `allocations` object (in `data/userdata.db`, `model_portfolios.allocations_json`):

```json
{
  "asset": { "equity": 60, "debt": 25, "gold": 5, "international": 10, "cash": 0 },
  "cap":   { "large": 45, "mid": 30, "small": 20, "micro": 5 },
  "direct_stock_pct": 25,
  "direct_cap": { "large": 60, "mid": 30, "small": 10, "micro": 0 }
}
```

- **`asset`** — asset-class allocation (%) to Equity, Debt, Gold, International, Cash.
- **`cap`** — within-equity cap split (%) into Large / Mid / Small / Micro.
- **`direct_stock_pct`** — how much of the portfolio is held as **direct stocks**.
- **`direct_cap`** — the same cap split applied to the direct-stock sleeve.

There are **no specific scheme/stock names** in a model — only allocation ranges. This is
the intended design: the model is the *target*, the strategy is the *limit set*, and
client portfolios are the *actuals*.

## Preset allocation templates

The Models editor ships five presets (also seeded by `webapp/seed_samples.py`):

| Preset | Equity | Debt | Gold | Intl | Large | Mid | Small | Micro | Direct |
|---|---|---|---|---|---|---|---|---|---|
| Balanced Growth | 60 | 25 | 5 | 10 | 45 | 30 | 20 | 5 | 25 |
| Equity Aggressive | 75 | 10 | 5 | 10 | 40 | 35 | 20 | 5 | 30 |
| Conservative | 25 | 65 | 5 | 5 | 70 | 20 | 10 | 0 | 10 |
| Income & Stability | 15 | 80 | 5 | 0 | 80 | 15 | 5 | 0 | 5 |
| Flexi Diversified | 60 | 20 | 5 | 15 | 40 | 30 | 25 | 5 | 25 |

## Storage & API

- **DB**: `data/userdata.db`, table `model_portfolios` (+ `allocations_json`). User-scoped.
- **Module**: `webapp/userdata.py` (create/update/get/list/delete).
- **Endpoints** (`webapp/tools_api.py`): `GET/POST /api/models`, `GET/PUT/DELETE /api/models/{id}`.
  Create/update bodies accept `{ name, strategy_id, items: [], allocations }`.
- A model can be **linked to a Strategy** (the limit set it should be checked against).

## Editor (UI)

Model Portfolios → **Models** tab:
- Custom **name** and **strategy** select.
- Asset-class %, cap split %, direct-stock % and direct-cap % inputs.
- A **Preset** dropdown loads any of the five allocation templates.
- No scheme/stock search — allocation targets only.