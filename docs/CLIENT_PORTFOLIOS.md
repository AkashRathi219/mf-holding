# Client Portfolios

Client portfolios are the portfolios **deployed to clients** — either a copy of a model
target or the client's **actual current holdings** (e.g. parsed from a CAS statement).

## Kinds

| Kind | Meaning |
|---|---|
| `model` | The client follows a **model target** (allocation plan copied from a model). |
| `actual` | The client's **actual current holdings** — a list of schemes/stocks with weights (e.g. from a CAS). |

`client_portfolios` rows carry both representations:
- **`items`** — a list of `{type: scheme|stock, id|name|isin, weight}` (actual holdings).
- **`allocations`** — an asset/cap plan (for `model` kind, copied from the deployed model).

## Deploying a model to clients

1. Create a **Model** (see `MODEL_PORTFOLIOS.md`).
2. Create a **Client** (`POST /api/clients`).
3. **Deploy** — `POST /api/client-portfolios` with `{ client_id, model_portfolio_id, kind }`.
   The client portfolio copies the model's items/allocations and its linked strategy.
   One model can be deployed to **many clients** (each gets its own instance).

## Importing actual holdings (CAS / manual)

For an **actual** position:
- Deploy a model (or create an empty client portfolio), then replace `items` with the
  client's real holdings — schemes and/or stocks with `weight` percentages.
- Example: `CAS_sample_portfolio_holdings.json` (a CAMS statement parse) is imported as
  an `actual` portfolio whose scheme items are matched to the DB by **ISIN or name**
  (plan/option suffixes such as `- Direct Plan - Growth` are stripped automatically).

## Storage & API

- **DB**: `data/userdata.db`, table `client_portfolios` (+ `allocations_json`). User-scoped.
- **Endpoints**: `GET/POST /api/client-portfolios`, `PUT/DELETE /api/client-portfolios/{id}`,
  `GET /api/clients`, `POST /api/clients`, `PUT/DELETE /api/clients/{id}`.
- Deleting a client also deletes its portfolios.

## Overview screen

Model Portfolios → **Overview** shows every client portfolio with its client, kind,
linked strategy, **compliance %** and pass/fail, plus an **Analyse** button that jumps to
the Analysis tab. The **Load sample data** button seeds strategies, models, clients and
deployments for the logged-in user (idempotent).