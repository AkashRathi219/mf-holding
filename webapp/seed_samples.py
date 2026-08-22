"""Seed sample strategies / model portfolios / clients / deployments.

Idempotent: only creates rows whose names are not already present for the user,
so it is safe to call repeatedly (and from ``POST /api/seed-samples``).
"""

from __future__ import annotations

from . import userdata
from .strategy_rules import parse_rules

SAMPLE_STRATEGIES = [
    {"name": "Conservative Core",
     "description": "Capital preservation; low single-name, sector and concentration risk.",
     "rules_text": ("Max 10% single stock. Max 20% sector. Min 30% debt. "
                    "Max 5% cash. Max top-5 25%. Max overlap 30%. Max 5 schemes. "
                    "Min 40% large cap. Max 30% mid cap. Max 10% small cap. Max 5% gold.")},
    {"name": "Balanced Growth",
     "description": "Moderate growth with a debt cushion.",
     "rules_text": ("Max 12% single stock. Max 25% sector. Min 20% debt. "
                    "Max 5% cash. Max top-5 30%. Max overlap 35%. Max 8 schemes. "
                    "Min 30% large cap. Max 25% mid cap. Max 15% small cap. Max 5% gold.")},
    {"name": "Equity Aggressive",
     "description": "Growth-first; tolerates higher concentration.",
     "rules_text": ("Max 15% single stock. Max 30% sector. Min 50% equity. "
                    "Max 3% cash. Max top-5 35%. Max overlap 40%. Max 10 schemes. "
                    "Min 20% large cap. Max 30% mid cap. Max 20% small cap. Max 5% microcap. Max 5% gold.")},
    {"name": "Income & Stability",
     "description": "Income-focused; heavy debt sleeve, low equity tolerance.",
     "rules_text": ("Max 8% single stock. Max 15% sector. Min 50% debt. "
                    "Max 5% cash. Max top-5 20%. Max overlap 25%. Max 6 schemes. "
                    "Max 30% equity. Max 40% large cap. Max 5% gold.")},
    {"name": "Flexi Diversified",
     "description": "Broad multi-asset mandate with international diversification.",
     "rules_text": ("Max 15% single stock. Max 30% sector. Min 10% international. "
                    "Max 5% cash. Max top-5 40%. Max overlap 45%. Max 12 schemes. "
                    "Min 40% equity. Min 20% large cap. Max 35% mid cap. Max 20% small cap. Max 10% gold.")},
]

SAMPLE_MODELS = [
    {"name": "Balanced Growth", "strategy_name": "Balanced Growth",
     "description": "Equity-oriented core with a debt cushion and modest gold/international.",
     "items": [],
     "allocations": {
        "asset": {"equity": 60, "debt": 25, "gold": 5, "international": 10, "cash": 0},
        "cap": {"large": 45, "mid": 30, "small": 20, "micro": 5},
        "direct_stock_pct": 25,
        "direct_cap": {"large": 60, "mid": 30, "small": 10, "micro": 0},
     }},
    {"name": "Equity Aggressive", "strategy_name": "Equity Aggressive",
     "description": "Growth-first with a wider mid/small sleeve and direct large-cap stocks.",
     "items": [],
     "allocations": {
        "asset": {"equity": 75, "debt": 10, "gold": 5, "international": 10, "cash": 0},
        "cap": {"large": 40, "mid": 35, "small": 20, "micro": 5},
        "direct_stock_pct": 30,
        "direct_cap": {"large": 60, "mid": 30, "small": 10, "micro": 0},
     }},
    {"name": "Conservative", "strategy_name": "Conservative Core",
     "description": "Debt-first with a small equity sleeve.",
     "items": [],
     "allocations": {
        "asset": {"equity": 25, "debt": 65, "gold": 5, "international": 5, "cash": 0},
        "cap": {"large": 70, "mid": 20, "small": 10, "micro": 0},
        "direct_stock_pct": 10,
        "direct_cap": {"large": 80, "mid": 20, "small": 0, "micro": 0},
     }},
    {"name": "Income & Stability", "strategy_name": "Income & Stability",
     "description": "Income-first with a large debt sleeve and low equity tolerance.",
     "items": [],
     "allocations": {
        "asset": {"equity": 15, "debt": 80, "gold": 5, "international": 0, "cash": 0},
        "cap": {"large": 80, "mid": 15, "small": 5, "micro": 0},
        "direct_stock_pct": 5,
        "direct_cap": {"large": 100, "mid": 0, "small": 0, "micro": 0},
     }},
    {"name": "Flexi Diversified", "strategy_name": "Flexi Diversified",
     "description": "Broad multi-asset mandate with meaningful international exposure.",
     "items": [],
     "allocations": {
        "asset": {"equity": 60, "debt": 20, "gold": 5, "international": 15, "cash": 0},
        "cap": {"large": 40, "mid": 30, "small": 25, "micro": 5},
        "direct_stock_pct": 25,
        "direct_cap": {"large": 60, "mid": 30, "small": 10, "micro": 0},
     }},
]

SAMPLE_CLIENTS = [
    {"name": "Rajesh", "org": "Acme", "notes": "Retired professional; capital preservation."},
    {"name": "Priya", "org": "Zenith", "notes": "Accumulation phase."},
    {"name": "Amit", "org": "", "notes": "High risk appetite."},
]

# "Actual" current position for Rajesh (drifts from the conservative target on purpose,
# so the compliance screen shows breaches to review).
RAJESH_ACTUAL_ITEMS = [
    {"type": "scheme", "id": 182, "name": "HDFC FLEXI CAP FUND", "weight": 30},
    {"type": "scheme", "id": 2015, "name": "SBI Nifty 50 ETF", "weight": 20},
    {"type": "stock", "isin": "INE002A01018", "name": "Reliance Industries Ltd.", "weight": 25},
    {"type": "stock", "isin": "INE040A01034", "name": "HDFC Bank Ltd.", "weight": 15},
    {"type": "stock", "isin": "INE009A01021", "name": "Infosys Limited", "weight": 10},
]

SAMPLE_DEPLOYMENTS = [
    {"model": "Balanced Growth", "client": "Rajesh", "kind": "model"},
    {"model": "Conservative", "client": "Priya", "kind": "model"},
    {"model": "Equity Aggressive", "client": "Amit", "kind": "model"},
]


def seed_for_user(uid: int) -> dict:
    created = {"strategies": [], "models": [], "clients": [], "deployments": []}

    strategies = {s["name"]: s["id"] for s in userdata.list_strategies(uid)}
    for s in SAMPLE_STRATEGIES:
        if s["name"] in strategies:
            continue
        st = userdata.create_strategy(uid, s["name"], s.get("description", ""), s["rules_text"])
        parsed = parse_rules(s["rules_text"])
        userdata.set_rules(uid, st["id"], parsed["rules"])
        strategies[s["name"]] = st["id"]
        created["strategies"].append(s["name"])

    models = {m["name"]: m["id"] for m in userdata.list_models(uid)}
    for m in SAMPLE_MODELS:
        if m["name"] in models:
            continue
        strat_id = strategies.get(m["strategy_name"])
        mm = userdata.create_model(uid, m["name"], m.get("description", ""), strat_id,
                                   m.get("items") or [], allocations=m.get("allocations"))
        models[m["name"]] = mm["id"]
        created["models"].append(m["name"])

    clients = {c["name"]: c["id"] for c in userdata.list_clients(uid)}
    for c in SAMPLE_CLIENTS:
        if c["name"] in clients:
            continue
        cl = userdata.create_client(uid, c["name"], c.get("org", ""), c.get("notes", ""))
        clients[c["name"]] = cl["id"]
        created["clients"].append(c["name"])

    existing_cp = {p["name"] for p in userdata.list_client_portfolios(uid)}

    for d in SAMPLE_DEPLOYMENTS:
        cid = clients.get(d["client"])
        mid = models.get(d["model"])
        if cid is None or mid is None:
            continue
        name = f"{d['model']} \u2014 {d['client']}"
        if name in existing_cp:
            continue
        model = userdata.get_model(uid, mid)
        userdata.create_client_portfolio(uid, cid, name, d.get("kind", "model"),
                                         model["items"], model_portfolio_id=mid,
                                         strategy_id=model.get("strategy_id"),
                                         allocations=model.get("allocations"))
        created["deployments"].append(name)

    # Rajesh's actual current position (vs the conservative strategy).
    rajesh_id = clients.get("Rajesh")
    if rajesh_id is not None and "Rajesh Actual" not in existing_cp:
        strat_id = strategies.get("Conservative Core")
        userdata.create_client_portfolio(uid, rajesh_id, "Rajesh Actual", "actual",
                                         RAJESH_ACTUAL_ITEMS, strategy_id=strat_id)
        created["deployments"].append("Rajesh Actual")

    return created