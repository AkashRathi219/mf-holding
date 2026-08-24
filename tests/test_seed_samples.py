"""Seed samples: Client 1 default portfolio from BOTH CAS reference files."""
from __future__ import annotations

import uuid

import pytest

from webapp import userdata
from webapp.seed_samples import (
    CAS_TRANSACTIONS_TXT,
    CLIENT1_NAME,
    DEFAULT_PORTFOLIO_NAME,
    MODEL_NAME,
    PORTFOLIO_NAME,
    cas_sample_items,
    cas_sample_transactions,
    cas_transaction_index,
    seed_for_user,
)

_needs_tx_file = pytest.mark.skipif(
    not CAS_TRANSACTIONS_TXT.exists(),
    reason="CAS_sample_extracted_transactions.txt not present")


def _fresh_uid() -> int:
    email = f"seed-{uuid.uuid4().hex[:8]}@test.local"
    from webapp import auth
    return int(auth.register_user(email, "Seeder", "", "password123")["user"]["id"])


def test_cas_items_structure():
    items = cas_sample_items()
    assert len(items) == 10  # 11 allocations minus the zero-unit line
    assert all({"type", "isin", "name", "units", "weight"} <= set(i) for i in items)
    assert sum(i["weight"] for i in items) == pytest.approx(100.0, abs=0.5)
    # zero-unit Kotak IDCW line must be dropped
    assert all(i["isin"] != "INF174K01FP0" for i in items)


@_needs_tx_file
def test_cas_items_enriched_from_transactions():
    idx = cas_transaction_index()
    assert len(idx) >= 20
    items = cas_sample_items()
    enriched = [i for i in items if i.get("amfi_code")]
    assert enriched, "holdings ISINs should join to transaction amfi codes"
    for i in enriched:
        ref = idx[i["isin"]]
        assert i["amfi_code"] == ref["amfi_code"]
        assert i["n_transactions"] == ref["n_txns"]


def test_seed_creates_client1_default_portfolio():
    uid = _fresh_uid()
    created = seed_for_user(uid)
    assert CLIENT1_NAME in created["clients"]
    assert DEFAULT_PORTFOLIO_NAME in created["portfolios"]

    clients = {c["name"]: c for c in userdata.list_clients(uid)}
    assert CLIENT1_NAME in clients
    models = {m["name"]: m["id"] for m in userdata.list_models(uid)}
    portfolios = {p["name"]: p for p in userdata.list_client_portfolios(uid)}
    dp = portfolios[DEFAULT_PORTFOLIO_NAME]
    assert dp["kind"] == "actual"
    assert dp["client_id"] == clients[CLIENT1_NAME]["id"]
    assert dp["model_portfolio_id"] == models[MODEL_NAME]
    assert len(dp["items"]) == len(cas_sample_items())
    # [ANA3 movement] BOTH seeded 'actual' portfolios carry the sample's
    # transactions (Demo on Default + CAS Sample portfolios)
    assert len(dp["transactions"]) == len(cas_sample_transactions())
    assert len(dp["transactions"]) > 800
    other = portfolios[PORTFOLIO_NAME]
    assert len(other["transactions"]) > 800


def test_seed_backfills_transactions_on_reseed():
    """A portfolio created before transactions_json existed gets backfilled
    when seed_for_user runs again (the live demo accounts' case)."""
    uid = _fresh_uid()
    seed_for_user(uid)
    con = userdata._conn()
    try:
        con.execute("UPDATE client_portfolios SET transactions_json='[]'")
        con.commit()
    finally:
        con.close()
    again = seed_for_user(uid)  # idempotent: nothing CREATED
    assert again["portfolios"] == []
    for p in userdata.list_client_portfolios(uid):
        if p["name"] in (PORTFOLIO_NAME, DEFAULT_PORTFOLIO_NAME):
            assert len(p["transactions"]) > 800


def test_seed_is_idempotent():
    uid = _fresh_uid()
    seed_for_user(uid)
    again = seed_for_user(uid)
    assert again["clients"] == []
    assert again["portfolios"] == []
    assert again["models"] == []
    assert len([p for p in userdata.list_client_portfolios(uid)
                if p["name"] == DEFAULT_PORTFOLIO_NAME]) == 1
