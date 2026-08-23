"""Seed the minimal demo dataset.

Per account, seeding produces EXACTLY:
  - 1 strategy : Full Coverage Playbook (12 rules across all dimensions)
  - 1 model    : CAS Sample Portfolio (holdings-level, from the CAS statement)
  - 2 clients  : Rajesh (primary demo) + Client 1 (default-portfolio client)
  - 2 portfolios: CAS Sample Portfolio (Rajesh's actuals) + Default Portfolio
                  (Client 1's actuals, same CAS holdings enriched with the
                  transaction extract)

Both reference files feed the structure:
  - CAS_sample_portfolio_holdings.json   -> per-scheme units + market weights
  - CAS_sample_extracted_transactions.txt -> per-ISIN amfi_code / txn counts

``seed_for_user(uid, reset=True)`` first REMOVES every existing
strategy/model/client/portfolio for that account, so demo accounts always end
in this clean state. Idempotent either way.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import userdata
from .strategy_rules import parse_rules

ROOT = Path(__file__).resolve().parent.parent
CAS_SAMPLE_JSON = ROOT / "CAS_sample_portfolio_holdings.json"
CAS_TRANSACTIONS_TXT = ROOT / "CAS_sample_extracted_transactions.txt"

STRATEGY_NAME = "Full Coverage Playbook"
MODEL_NAME = "CAS Sample Portfolio"
CLIENT_NAME = "Rajesh"
PORTFOLIO_NAME = "CAS Sample Portfolio"
CLIENT1_NAME = "Client 1"
DEFAULT_PORTFOLIO_NAME = "Default Portfolio"

SAMPLE_STRATEGIES = [
    {"name": STRATEGY_NAME,
     "description": ("Demonstration mandate exercising all 12 rule dimensions: "
                     "single-holding, sector, debt/equity/cash/gold bands, "
                     "large/mid/small-cap bands, top-5 concentration, scheme "
                     "overlap and scheme count."),
     "rules_text": ("Max 15% single stock. Max 25% sector. "
                    "Min 25% debt. Max 60% equity. Max 8% cash. Max 5% gold. "
                    "Min 30% large cap. Max 30% mid cap. Max 12% small cap. "
                    "Max top-5 60%. Max overlap 50%. Max 12 schemes.")},
]

SAMPLE_CLIENTS = [
    {"name": CLIENT_NAME, "org": "", "notes": "Primary demo client."},
]


def cas_transaction_index() -> dict[str, dict]:
    """Structure reference #2: per-ISIN metadata from the transaction extract.

    Returns ``{isin: {amfi_code, n_txns, first_date, last_date}}`` aggregated
    across all 907 extracted transactions."""
    try:
        doc = json.loads(CAS_TRANSACTIONS_TXT.read_text(encoding="utf-8"))
    except Exception:
        return {}
    idx: dict[str, dict] = {}
    for t in doc.get("transactions") or []:
        isin = (t.get("isin") or "").strip().upper()
        if not isin:
            continue
        e = idx.setdefault(isin, {"amfi_code": "", "n_txns": 0,
                                  "first_date": None, "last_date": None})
        e["n_txns"] += 1
        code = (t.get("amfi_code") or "").strip()
        if code and not e["amfi_code"]:
            e["amfi_code"] = code
        d = t.get("date")
        if d:
            if not e["first_date"] or d < e["first_date"]:
                e["first_date"] = d
            if not e["last_date"] or d > e["last_date"]:
                e["last_date"] = d
    return idx


def cas_sample_items() -> list[dict]:
    """Portfolio items from the CAS sample statement (market-value weights).

    Items carry isin + name so they resolve against the schemes table for BOTH
    compliance analysis and scheme overlap; ``amfi_code``/txn stats are joined
    from the transactions extract where available. Zero-unit / unpriced lines
    are dropped; weights renormalise to 100."""
    try:
        doc = json.loads(CAS_SAMPLE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    txidx = cas_transaction_index()
    items: list[dict] = []
    for a in (doc.get("portfolio_summary") or {}).get("allocations") or []:
        name = (a.get("scheme_name") or "").strip()
        isin = (a.get("isin") or "").strip().upper()
        weight = a.get("allocation_pct_market_value")
        units = a.get("net_units")
        if not name or not isin or not weight or not units:
            continue
        item = {"type": "scheme", "isin": isin, "name": name,
                "units": float(units), "weight": round(float(weight), 2)}
        ref = txidx.get(isin)
        if ref:
            item["amfi_code"] = ref["amfi_code"]
            item["n_transactions"] = ref["n_txns"]
        items.append(item)
    total = sum(i["weight"] for i in items)
    if total > 0:
        for i in items:
            i["weight"] = round(i["weight"] / total * 100, 2)
    return items


def _reset_account(uid: int) -> None:
    """Remove ALL strategies/models/clients/portfolios for this account."""
    con = userdata._conn()
    try:
        for table in ("analysis_runs", "client_portfolios", "model_portfolios",
                      "clients", "strategies"):
            con.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        con.execute("DELETE FROM rules WHERE strategy_id NOT IN "
                    "(SELECT id FROM strategies)")
        con.commit()
    finally:
        con.close()


def seed_for_user(uid: int, reset: bool = False) -> dict:
    created = {"strategies": [], "models": [], "clients": [], "portfolios": []}

    if reset:
        _reset_account(uid)

    # ---- 1 strategy ----
    strategies = {s["name"]: s["id"] for s in userdata.list_strategies(uid)}
    if STRATEGY_NAME not in strategies:
        spec = SAMPLE_STRATEGIES[0]
        st = userdata.create_strategy(uid, spec["name"], spec["description"],
                                      spec["rules_text"])
        parsed = parse_rules(spec["rules_text"])
        userdata.set_rules(uid, st["id"], parsed["rules"])
        strategies[spec["name"]] = st["id"]
        created["strategies"].append(spec["name"])

    # ---- clients ----
    clients = {c["name"]: c["id"] for c in userdata.list_clients(uid)}
    if CLIENT_NAME not in clients:
        cl = userdata.create_client(uid, CLIENT_NAME, "",
                                    "Primary demo client.")
        clients[CLIENT_NAME] = cl["id"]
        created["clients"].append(CLIENT_NAME)
    if CLIENT1_NAME not in clients:
        cl = userdata.create_client(
            uid, CLIENT1_NAME, "",
            "Default client seeded from the CAS sample statement + "
            "transaction extract.")
        clients[CLIENT1_NAME] = cl["id"]
        created["clients"].append(CLIENT1_NAME)

    # ---- 1 model + 2 client portfolios from the CAS statement ----
    cas_items = cas_sample_items()
    models = {m["name"]: m["id"] for m in userdata.list_models(uid)}
    if cas_items and MODEL_NAME not in models:
        mdl = userdata.create_model(uid, MODEL_NAME,
                                    "Holdings-level model built from the CAS "
                                    "sample statement (market-value weights).",
                                    strategies.get(STRATEGY_NAME), cas_items)
        models[MODEL_NAME] = mdl["id"]
        created["models"].append(MODEL_NAME)

    existing_cp = {p["name"] for p in userdata.list_client_portfolios(uid)}
    if cas_items and PORTFOLIO_NAME not in existing_cp:
        userdata.create_client_portfolio(
            uid, clients.get(CLIENT_NAME), PORTFOLIO_NAME, "actual", cas_items,
            model_portfolio_id=models.get(MODEL_NAME),
            strategy_id=strategies.get(STRATEGY_NAME))
        created["portfolios"].append(PORTFOLIO_NAME)
    if cas_items and DEFAULT_PORTFOLIO_NAME not in existing_cp:
        userdata.create_client_portfolio(
            uid, clients.get(CLIENT1_NAME), DEFAULT_PORTFOLIO_NAME,
            "actual", cas_items,
            model_portfolio_id=models.get(MODEL_NAME),
            strategy_id=strategies.get(STRATEGY_NAME))
        created["portfolios"].append(DEFAULT_PORTFOLIO_NAME)

    _heal_model_links(uid, models)
    return created


def _heal_model_links(uid: int, models: dict) -> None:
    """Re-point legacy client portfolios whose model link predates the fixed
    model-id lookup (they were created with ``model_portfolio_id=NULL`)."""
    model_id = models.get(MODEL_NAME)
    if not model_id:
        return
    con = userdata._conn()
    try:
        con.execute(
            "UPDATE client_portfolios SET model_portfolio_id=? "
            "WHERE user_id=? AND model_portfolio_id IS NULL AND name=?",
            (model_id, uid, PORTFOLIO_NAME))
        con.commit()
    finally:
        con.close()
