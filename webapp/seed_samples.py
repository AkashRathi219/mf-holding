"""Seed sample strategies / model portfolios / clients / deployments.

Idempotent: only creates rows whose names are not already present for the user,
so it is safe to call repeatedly (and from ``POST /api/seed-samples``).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import userdata
from .strategy_rules import parse_rules

ROOT = Path(__file__).resolve().parent.parent
CAS_SAMPLE_JSON = ROOT / "CAS_sample_portfolio_holdings.json"

# One strategy that exercises EVERY analyser dimension (single-holding,
# sector, asset-class min/max incl. debt/equity/cash/gold, cap segments,
# concentration, scheme overlap and scheme count). Pairs with the CAS Sample
# Portfolio below so compliance shows a realistic pass/breach mix.
SAMPLE_STRATEGIES = [
    {"name": "Full Coverage Playbook",
     "description": ("Demonstration mandate exercising all 12 rule dimensions: "
                     "single-holding, sector, debt/equity/cash/gold bands, "
                     "large/mid/small-cap bands, top-5 concentration, scheme "
                     "overlap and scheme count."),
     "rules_text": ("Max 15% single stock. Max 25% sector. "
                    "Min 25% debt. Max 60% equity. Max 8% cash. Max 5% gold. "
                    "Min 30% large cap. Max 30% mid cap. Max 12% small cap. "
                    "Max top-5 60%. Max overlap 50%. Max 12 schemes.")},
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


def cas_sample_items() -> list[dict]:
    """Portfolio items from the CAS sample statement (market-value weights).

    Items carry isin + name so they resolve against the schemes table for BOTH
    compliance analysis and scheme overlap. Zero-unit / unpriced lines are
    dropped; weights renormalise to 100."""
    try:
        doc = json.loads(CAS_SAMPLE_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    items: list[dict] = []
    for a in (doc.get("portfolio_summary") or {}).get("allocations") or []:
        name = (a.get("scheme_name") or "").strip()
        isin = (a.get("isin") or "").strip().upper()
        weight = a.get("allocation_pct_market_value")
        units = a.get("net_units")
        if not name or not isin or not weight or not units:
            continue
        items.append({"type": "scheme", "isin": isin, "name": name,
                      "units": float(units), "weight": round(float(weight), 2)})
    total = sum(i["weight"] for i in items)
    if total > 0:
        for i in items:
            i["weight"] = round(i["weight"] / total * 100, 2)
    return items


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

    # CAS Sample Portfolio — built from the real CAS statement holdings, linked
    # to the Full Coverage Playbook strategy so Analyse (compliance + debt
    # analysis) and Overlap both work out of the box.
    cas_items = cas_sample_items()
    if cas_items and "CAS Sample Portfolio" not in existing_cp:
        rajesh_id = clients.get("Rajesh")
        if rajesh_id is not None:
            userdata.create_client_portfolio(
                uid, rajesh_id, "CAS Sample Portfolio", "actual", cas_items,
                strategy_id=strategies.get("Full Coverage Playbook"))
            created["deployments"].append("CAS Sample Portfolio")
        models = {m["name"]: m["id"] for m in userdata.list_models(uid)}
        if "CAS Sample Portfolio" not in models:
            userdata.create_model(uid, "CAS Sample Portfolio",
                                  "Holdings-level model built from the CAS sample "
                                  "statement (market-value weights).",
                                  strategies.get("Full Coverage Playbook"),
                                  cas_items)
            created["models"].append("CAS Sample Portfolio")

    return created