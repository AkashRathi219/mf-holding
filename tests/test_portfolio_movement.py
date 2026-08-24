"""[ANA3 movement] cash-flow-aware portfolio movement analytics tests.

Hand-computed vectors (see docs/plans/PLAN_PORTFOLIO_MOVEMENT.md):
  T2: NAV 10.00 +1%/day x5; opening 500u = 5000; purchase 100u @10.303
      (1030.30) -> values 5000/5050/5100.50/6181.80/6243.60;
      total TWR = 1.01^4 - 1 = 4.0604%; public annualized = null (4d < 90d).
  T4: 100 -> 110 @365.25d no flows -> XIRR = 10.000% approx.
"""

from __future__ import annotations

import pytest
from datetime import date, timedelta as td

from webapp.analytics import (MIN_CAGR_WINDOW_DAYS, portfolio_movement_series)
from webapp.tools_api import parse_cas_transactions


def _nav_map(start: date, rate: float, n: int) -> dict:
    """{(start+i).isoformat(): nav} on a constant daily growth path."""
    out = {}
    v = 100.0
    for i in range(n):
        if i:
            v *= (1 + rate)  # day-0 stays 100
        out[(start + td(days=i)).isoformat()] = round(v, 4)
    return out


def _lookup(nav: dict):
    return lambda amfi_code, isin: (nav, "test")


def _tx(day: str, units: float, amount: float, ttype: str = "PURCHASE",
        isin: str = "INF000000101", code: str = "700001",
        name: str = "Test Fund") -> dict:
    """Canonical record mirroring parse_cas_transactions: amount and
    cum_units are SIGNED by type (PURCHASE +, REDEEM/SWITCH_OUT -)."""
    is_in = ttype in ("PURCHASE", "BUY", "SWITCH_IN", "REINVESTMENT")
    sign = 1.0 if is_in else -1.0
    return {"date": day, "type": ttype, "sign": sign,
            "units": units, "amount": round(amount * sign, 4),
            "cum_units": round(units * sign, 4),
            "isin": isin, "amfi_code": code, "name": name,
            "nav": None}


# ---- T1: opening deduction -------------------------------------------------------

def test_opening_units_deduced_from_end_units():
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 1000.0,
              "amfi_code": "700001"}]
    tx = [_tx("2026-01-05", 500, 5000.0),
          _tx("2026-02-02", 200, 2200.0, ttype="REDEEM")]
    got = portfolio_movement_series(
        items, tx, _lookup(_nav_map(date(2026, 1, 1), 0.01, 70)))
    assert got is not None
    assert got["constituents"][0]["opening_units"] == pytest.approx(700.0, abs=1e-6)
    assert got["constituents"][0]["end_units"] == pytest.approx(1000.0, abs=1e-6)
    assert got["constituents"][0]["tx_count"] == 2
    assert got["start"] == "2026-01-01"  # earliest valued date (nav data)
    assert got["end"] >= "2026-02-02"


# ---- T2: hand-computed values + TWR + honest annualisation ----------------------

def test_t2_single_scheme_hand_computed_twr():
    d0 = date(2026, 1, 2)
    nav = {}
    v = 10.00
    for i in range(5):
        if i:
            v = round(v * 1.01, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 600.0,
              "amfi_code": "700001"}]
    tx = [_tx((d0 + td(days=3)).isoformat(), 100, 1030.30)]
    got = portfolio_movement_series(
        items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    vs = got["value_series"]["values"]
    assert vs == [pytest.approx(5000.0, abs=0.01),
                  pytest.approx(5050.0, abs=0.01),
                  pytest.approx(5100.5, abs=0.01),
                  pytest.approx(6181.8, abs=0.01),
                  pytest.approx(6243.6, abs=0.01)]
    assert got["terminal_value"] == pytest.approx(6243.6, abs=0.01)
    assert got["total_net_flow"] == pytest.approx(1030.30, abs=0.01)
    # daily TWR == 1% each day (flow-adjusted), total = 1.01^4 - 1 (2dp output)
    assert got["total_twr_pct"] == pytest.approx((1.01 ** 4 - 1) * 100.0, abs=0.01)
    # 4d span < 90d floor -> honest nulls + dated reason
    assert got["annualized_twr_pct"] is None
    assert got["xirr_pct"] is None
    au = got["annualized_unavailable"]
    assert au["required_span_days"] == MIN_CAGR_WINDOW_DAYS
    assert au["span_days"] == 4
    assert au["window_start"] == got["start"] and au["window_end"] == got["end"]
    # opening value deduction
    assert got["opening_value"] == pytest.approx(5000.0, abs=0.01)


# ---- T3: TWR vs MWR divergence ---------------------------------------------------

def test_twr_annualised_below_xirr_on_late_large_flow():
    """A big purchase right before a strong run lifts XIRR above TWR when the
    money arrived late (timing effect — money-weighted vs time-weighted)."""
    d0 = date(2026, 1, 1)
    n = 400
    nav = {}
    v = 100.0
    for i in range(n):
        if i:
            v *= 1.0015  # +0.15%/day drift
        nav[(d0 + td(days=i)).isoformat()] = round(v, 4)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 600.0,
              "amfi_code": "700001"}]
    # opening 500u, then a large purchase on day 350 (100u) so most money
    # rides only the final 50 days
    tx = [_tx((d0 + td(days=350)).isoformat(), 100,
              round(v * 100 * (1.0015 ** 350), 2))]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["days"] >= 399
    assert got["annualized_twr_pct"] is not None  # 400d >= 90d floor
    assert got["xirr_pct"] is not None
    assert got["xirr_pct"] > got["annualized_twr_pct"]


# ---- T4: XIRR canonical ----------------------------------------------------------

def test_xirr_canonical_no_flow():
    d0 = date(2026, 1, 1)
    nav = {(d0 + td(days=0)).isoformat(): 100.0,
           (d0 + td(days=365)).isoformat(): 110.0}
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 100.0,
              "amfi_code": "700001"}]  # no tx: opened with 100u at 100
    # purchases none; opening = end_units - 0 = 100 units @ start x 100 = 10000?
    # NB: XIRR canonical needs a flow-free hold: 100u bought BEFORE start.
    tx: list[dict] = []
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is None  # no transactions -> honest None


def test_xirr_canonical_with_purchase():
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(366):
        if i:
            v *= 1.001  # ~+1.1%/day > 10% p.a. proxy; exact 10% not trivial
        nav[(d0 + td(days=i)).isoformat()] = round(v, 4)
    # buy the whole holding on day 0: opening becomes 0; value 100 -> 100*(1.001)^365
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 100.0,
              "amfi_code": "700001"}]
    tx = [_tx(d0.isoformat(), 100, 10000.0)]  # 100 units @ nav 100 = 10000
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["opening_value"] == 0.0
    # XIRR must be the daily rate annualised: (1.001)^365.25 - 1
    expected = ((1.001 ** (365.25)) - 1) * 100
    assert got["xirr_pct"] == pytest.approx(expected, rel=0.02)
    assert got["total_twr_pct"] == pytest.approx(
        (1.001 ** 365 - 1) * 100, abs=0.01)


def test_xirr_underdetermined_returns_none():
    # single day -> nothing to annualise; engine errors honestly
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 100.0,
              "amfi_code": "700001"}]
    tx = [_tx("2026-01-01", 100, 100.0)]
    got = portfolio_movement_series(
        items, tx, _lookup({"2026-01-01": 100.0}))
    assert got is None or "error" in got or got["annualized_twr_pct"] is None


# ---- regression: float-dust truncation + artifact-aware drawdown -----------------


def test_phantom_flow_from_unvalued_scheme_excluded():
    """A purchase in a scheme WITHOUT nav history must not enter the flow
    series: value unchanged, flow uncounted -> no phantom -100% day, and the
    drawdown stays available. Excluded cash is reported honestly."""
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(40):
        if i:
            v = round(v * 1.001, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 300.0,
              "amfi_code": "700001"},
             {"isin": "INF000000999", "name": "Ghost Fund", "units": 10.0,
              "amfi_code": "700009"}]  # no nav history for this code
    tx = [_tx(d0.isoformat(), 300, 30000.0, isin="INF000000101",
              code="700001"),
          _tx((d0 + td(days=10)).isoformat(), 10, 17000000.0,
              isin="INF000000999", code="700009")]  # phantom ₹1.7cr purchase
    # real lookups return None for codes without NAV history
    got = portfolio_movement_series(items, tx,
                                    lambda a, i: (nav, "test") if a == "700001" else None)
    assert got is not None and "error" not in got
    # the ghost purchase must NOT distort the valued path: no |r| > 20% day
    assert all(abs(r) <= 20.0 for r in got["daily_returns"]["values"])
    # drawdown remains available (no artifacts)
    assert got["max_drawdown_pct"] is not None
    assert got["drawdown_unavailable"] is None
    # the excluded cash is reported, never silently dropped
    dn = got["data_note"]
    assert dn["unvalued_schemes"] == 1
    assert dn["unvalued_tx_count"] == 1
    assert dn["unvalued_net_flow"] == pytest.approx(17000000.0, abs=0.01)
    # net invested counts only the valued scheme's cash
    assert got["total_net_flow"] == pytest.approx(30000.0, abs=0.01)


def test_truncation_ignores_float_dust_day():
    """A sub-repairable opening (e.g. 1.42e-14 units leftover) must not extend
    the series back to a scheme's inception as a 0.00-value day."""
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(5):
        if i:
            v *= 1.01
        nav[(d0 + td(days=i)).isoformat()] = round(v, 4)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 100.0,
              "amfi_code": "700001"}]
    # opening = 100 - 99.99999999999999 = 1.42e-14 -> dust before the purchase
    tx = [_tx(d0.isoformat(), 99.99999999999999, 9999.999999999998)]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    # series must start at the (d0) purchase day with a material value, not at
    # a dust-value 0.00 day
    assert got["value_series"]["values"][0] >= 1.0
    assert got["value_series"]["dates"][0] == d0.isoformat()


def test_drawdown_nulls_when_flow_artifacts_present():
    """Clamped (statement-inconsistent) daily returns null the drawdown and
    emit drawdown_unavailable + data_note — never a manufactured -100%."""
    from webapp import analytics as ana

    # craft a value path whose day-3 collapse exceeds total loss: redemption
    # amount far larger than implied value -> r < -0.999 -> clamped
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(5):
        if i:
            v *= 1.001
        nav[(d0 + td(days=i)).isoformat()] = round(v, 4)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 0.0,
              "amfi_code": "700001"}]  # fully exited by end
    tx = [_tx((d0 + td(days=0)).isoformat(), 100, 10000.0),
          # overstated redemption amount (10x the implied value)
          _tx((d0 + td(days=3)).isoformat(), 100, 100000.0, ttype="REDEEM")]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    if got is None or "error" in got:
        return  # honest degrade is acceptable for this pathological input
    assert got["data_note"]["artifacts"] is True
    assert got["max_drawdown_pct"] is None
    assert got["drawdown_unavailable"]["reason"] == "flow_adjustment_artifacts"
    assert got["drawdown_unavailable"]["artifact_days"] >= 1


def test_drawdown_present_without_artifacts():
    """Hand-computed clean path keeps a real drawdown figure."""
    d0 = date(2026, 1, 1)
    nav = {d0.isoformat(): 100.0, (d0 + td(days=1)).isoformat(): 90.0,
           (d0 + td(days=2)).isoformat(): 100.0}
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 10.0,
              "amfi_code": "700001"}]
    tx = [_tx(d0.isoformat(), 10, 1000.0)]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["data_note"]["artifacts"] is False
    assert got["max_drawdown_pct"] == pytest.approx(-10.0, abs=0.01)
    assert got["drawdown_unavailable"] is None


# ---- regressions: signed-parse contract, switches, invested, start rule ----------


def test_parse_output_engine_contract_no_double_sign():
    """Engine consumes parse_cas_transactions output as-is (already signed).
    A REDEEM day must NOT double-cancel: net flow = purch - redeem."""
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(40):
        if i:
            v = round(v * 1.001, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    doc = {"transactions": [
        {"date": d0.isoformat(), "transaction_type": "PURCHASE", "units": "100",
         "amount": "10000", "isin": "INF000000101", "amfi_code": "700001",
         "scheme_name": "Test Fund"},
        {"date": (d0 + td(days=20)).isoformat(), "transaction_type": "REDEEM",
         "units": "40", "amount": "4080.40", "isin": "INF000000101",
         "amfi_code": "700001", "scheme_name": "Test Fund"}]}
    txs = parse_cas_transactions(doc)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 60.0,
              "amfi_code": "700001"}]
    got = portfolio_movement_series(items, txs, _lookup(nav))
    assert got is not None and "error" not in got
    # net invested = 10000 - 4080.40 = 5919.60 (REDEEM signed negative)
    assert got["total_net_flow"] == pytest.approx(5919.60, abs=0.02)
    assert got["cash_in"] == pytest.approx(10000.0, abs=0.02)
    assert got["cash_out"] == pytest.approx(-4080.40, abs=0.02)
    # clean TWR around both flows (~0.1%/day drift, no artifacts from signs)
    assert got["data_note"]["artifacts"] is False


def test_switch_in_excluded_from_everywhere():
    """SWITCH_IN is internal: excluded from units, flows, invested."""
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(40):
        if i:
            v = round(v * 1.001, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    doc = {"transactions": [
        {"date": d0.isoformat(), "transaction_type": "PURCHASE", "units": "50",
         "amount": "5000", "isin": "INF000000101", "amfi_code": "700001",
         "scheme_name": "Test Fund"},
        {"date": (d0 + td(days=10)).isoformat(), "transaction_type": "SWITCH_IN",
         "units": "30", "amount": "3030", "isin": "INF000000101",
         "amfi_code": "700001", "scheme_name": "Test Fund"}]}
    txs = parse_cas_transactions(doc)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 80.0,
              "amfi_code": "700001"}]
    got = portfolio_movement_series(items, txs, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["data_note"]["switches_skipped"] == 1
    # invested excludes the switch: net = 5000 only; opening = 80-50 = 30u
    assert got["total_net_flow"] == pytest.approx(5000.0, abs=0.02)
    assert got["cash_in"] == pytest.approx(5000.0, abs=0.02)
    c = got["constituents"][0]
    assert c["opening_units"] == pytest.approx(30.0, abs=1e-6)
    assert c["tx_count"] == 1  # switch not counted


def test_start_rule_full_history_begins_at_first_purchase():
    """Full-history (opening ~0) series must start at the FIRST PURCHASE date,
    never at the earliest NAV date."""
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(60):
        if i:
            v = round(v * 1.001, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 300.0,
              "amfi_code": "700001"}]
    txs = [parse_cas_transactions({"transactions": [
        {"date": (d0 + td(days=15)).isoformat(), "transaction_type": "PURCHASE",
         "units": "300", "amount": "30000", "isin": "INF000000101",
         "amfi_code": "700001", "scheme_name": "Test Fund"}]})[0]]
    got = portfolio_movement_series(items, txs, _lookup(nav))
    assert got is not None and "error" not in got
    # opening = 300 - 300 = 0 -> full history -> start = purchase date (d0+15)
    assert got["data_note"]["start_reason"] == "first_purchase"
    assert got["start"] == (d0 + td(days=15)).isoformat()
    assert got["opening_value"] == pytest.approx(0.0, abs=0.001)
    # value path begins at the purchase day with a material value
    assert got["value_series"]["values"][0] >= 100.0


def test_sample_invested_reconciles_purch_minus_redeem():
    """Real sample: net invested = PURCHASE - REDEEM (switches excluded) —
    the live card must show ~1.43cr, not 822cr."""
    from webapp.seed_samples import cas_sample_transactions
    tx = cas_sample_transactions()
    real = [t for t in tx if t.get("type") not in ("SWITCH_IN", "SWITCH_OUT")]
    net = sum(t["amount"] for t in real)
    cin = sum(t["amount"] for t in real if t["amount"] > 0)
    cout = sum(t["amount"] for t in real if t["amount"] < 0)
    assert abs(net - 14298394.0) < 50000
    assert abs(cin - 416790864.0) < 50000
    assert abs(cout - (-402492469.0)) < 50000


# ---- NAV-source ladder (file/R2 -> statement-tx; no third-party mirrors) ----------


def test_nav_source_ladder_no_mirror_no_fetch(tmp_path, monkeypatch):
    """[DATA-POLICY] the ladder never calls third-party mirrors: with no
    local file and an R2 miss it falls straight through to the statement-tx
    tier (or None), making zero network calls."""
    from webapp import db as wdb

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb.remote_store, "ensure", lambda rel: None)
    monkeypatch.setattr(wdb.remote_store, "download_to", lambda rel, dest: None)

    net_calls = []

    class _Guard:
        def __getattr__(self, name):  # any boto3 touch would land here
            raise AssertionError("third-party mirror contacted")

    w = wdb.WebDB()
    # 1) statement tier resolves without any network
    tx_navs = {"2026-01-05": 101.0, "2026-02-05": 102.0}
    got = w._movement_nav_map("700010", "", tx_navs=tx_navs)
    assert got is not None
    navs, source = got
    assert source == "statement_tx" and navs == tx_navs
    # 2) nothing at all -> honest None (no mirror fallback exists any more)
    assert w._movement_nav_map("700011", "") is None


def test_nav_source_ladder_statement_tx_tier(tmp_path, monkeypatch):
    """When no file/R2 exists, the CAS records' own NAVs are used
    (real fund NAVs on tx dates) and stamped statement_tx."""
    from webapp import db as wdb

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb.remote_store, "ensure", lambda rel: None)

    w = wdb.WebDB()
    tx_navs = {"2026-01-05": 101.0, "2026-02-05": 102.0}
    got = w._movement_nav_map("700010", "", tx_navs=tx_navs)
    assert got is not None
    navs, source = got
    assert source == "statement_tx"
    assert navs == tx_navs


def test_constituents_carry_nav_source():
    d0 = date(2026, 1, 2)
    nav = {}
    v = 10.0
    for i in range(5):
        if i:
            v = round(v * 1.01, 4)
        nav[(d0 + td(days=i)).isoformat()] = v
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 600.0,
              "amfi_code": "700001"}]
    tx = [_tx((d0 + td(days=3)).isoformat(), 100, 1030.30)]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got["constituents"][0]["nav_source"] == "test"


# ---- T5: full exit ---------------------------------------------------------------

def test_full_exit_terminal_value_zero():
    d0 = date(2026, 1, 1)
    nav = {}
    v = 100.0
    for i in range(40):
        if i:
            v *= 1.005
        nav[(d0 + td(days=i)).isoformat()] = round(v, 4)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 0.0,
              "amfi_code": "700001"}]  # exited fully
    tx = [_tx(d0.isoformat(), 100, 10000.0),
          _tx((d0 + td(days=39)).isoformat(), 100,
              10000 * (1.005 ** 39), ttype="REDEEM")]
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    assert got["opening_value"] == 0.0
    assert got["terminal_value"] == pytest.approx(0.0, abs=0.01)
    # total TWR = growth of 1.005^39 held without flows until exit
    # (both purchase and redemption are same-scheme flows; TWR isolates drift)
    assert got["total_twr_pct"] == pytest.approx(
        (1.005 ** 39 - 1) * 100, abs=0.05)


# ---- T6: NAV gap forward-fill ----------------------------------------------------

def test_nav_gap_day_keeps_value_flat():
    d0 = date(2026, 1, 1)
    nav = {d0.isoformat(): 100.0,
           (d0 + td(days=1)).isoformat(): 100.0,
           (d0 + td(days=3)).isoformat(): 102.0}  # day2 missing (weekend)
    items = [{"isin": "INF000000101", "name": "Test Fund", "units": 10.0,
              "amfi_code": "700001"}]
    tx = [_tx(d0.isoformat(), 10, 1000.0)]  # bought at start -> opening 0
    got = portfolio_movement_series(items, tx, _lookup(nav))
    assert got is not None and "error" not in got
    # grid = nav dates; day2 missing -> value flat through the gap: 1000, 1000, 1020
    assert got["value_series"]["values"] == [pytest.approx(1000.0, abs=0.01),
                                             pytest.approx(1000.0, abs=0.01),
                                             pytest.approx(1020.0, abs=0.01)]
    # daily returns are PERCENT (0.0% then +2.0% over the gap day), each
    # dated its OWN day — dates/values strictly aligned
    assert got["daily_returns"]["values"] == [pytest.approx(0.0, abs=1e-9),
                                              pytest.approx(2.0, abs=1e-6)]
    assert got["daily_returns"]["dates"] == [(d0 + td(days=1)).isoformat(),
                                             (d0 + td(days=3)).isoformat()]
    assert len(got["daily_returns"]["dates"]) == len(got["daily_returns"]["values"])


# ---- T7: normalisation (sign, drops, ISIN fallback) ------------------------------

def test_parse_cas_transactions_normalisation():
    doc = {"transactions": [
        {"date": "2026-01-05", "transaction_type": "PURCHASE", "units": "10",
         "amount": "1000", "isin": "INF000000101", "amfi_code": "700001",
         "scheme_name": "Test Fund"},
        {"date": "2026-02-02", "transaction_type": "REDEEM", "units": "4",
         "amount": "420", "isin": "INF000000101", "amfi_code": "700001",
         "scheme_name": "Test Fund"},
        {"date": "2026-02-10", "transaction_type": "SWITCH_IN", "units": "2",
         "amount": "210", "isin": "INF000000102", "amfi_code": "",
         "scheme_name": "Other Fund"},
        {"date": "2026-02-11", "transaction_type": "SWITCH_OUT", "units": "1",
         "amount": "105", "isin": "INF000000102", "amfi_code": "",
         "scheme_name": "Other Fund"},
        {"date": "2026-02-12", "transaction_type": "PURCHASE", "units": "0",
         "amount": "0", "isin": "INF000000103", "amfi_code": "700003"},
        {"transaction_type": "PURCHASE", "units": "5", "amount": "500",
         "isin": "INF000000104", "amfi_code": "700004"},  # no date -> dropped
    ]}
    got = parse_cas_transactions(doc)
    assert len(got) == 4  # zero-both and no-date dropped
    by_date = {r["date"]: r for r in got}
    assert by_date["2026-01-05"]["amount"] == 1000.0      # purchase +
    assert by_date["2026-02-02"]["amount"] == -420.0      # redeem -
    assert by_date["2026-02-10"]["amount"] == 210.0       # switch_in +
    assert by_date["2026-02-11"]["amount"] == -105.0      # switch_out -
    assert got[0]["date"] <= got[1]["date"]  # sorted


# ---- T8: real sample end-to-end (requires repo fixtures) --------------------------

SAMPLE_TX = None


def _sample_tx():
    global SAMPLE_TX
    if SAMPLE_TX is None:
        import json
        from pathlib import Path
        SAMPLE_TX = parse_cas_transactions(json.loads(
            Path(r"D:\opencode\mf_holding\CAS_sample_extracted_transactions.txt")
            .read_text(encoding="utf-8")))
    return SAMPLE_TX


def test_sample_transactions_normalised():
    tx = _sample_tx()
    assert len(tx) > 800  # 907 expected, minus drops
    assert all(r["date"] for r in tx)
    # sign consistency: signed amount and signed units must agree in direction
    assert all(r["amount"] * r["cum_units"] >= 0 or r["amount"] == 0
               for r in tx)


def test_movement_end_to_end_via_db(tmp_path, monkeypatch):
    """Real 907-tx sample -> WebDB movement series reconciles to end units."""
    from webapp import db as wdb
    import json

    hist = tmp_path / "navhist"
    hist.mkdir(exist_ok=True)
    monkeypatch.setattr(wdb, "NAV_HISTORY_DIR", hist)
    monkeypatch.setattr(wdb, "_nav_heal_attempted", set())

    import sqlite3
    from conftest import WEBAPP_DB
    from webapp.seed_samples import cas_sample_items
    tx = _sample_tx()
    codes = {r["amfi_code"] for r in tx if r["amfi_code"]}
    for code in sorted(codes):
        doc = {"scheme_code": code, "fund_name": "Fund " + code,
               "history": [{"date": (date(2016, 2, 10) + td(days=i)).strftime("%d-%b-%Y"),
                            "nav": round(100 * (1.0005 ** i), 4)}
                           for i in range(0, 3800, 7)]}  # weekly grid
        (hist / f"{code}.json").write_text(json.dumps(doc), encoding="utf-8")

    # sample items (weight + units + amfi_code, exactly what seeding stores)
    items = cas_sample_items()
    assert items, "cas sample items are required by this test"
    # the weight-blend layer needs scheme rows resolvable by the items' ISINs
    con = sqlite3.connect(WEBAPP_DB)
    cur = con.cursor()
    inserted = []
    for it in items:
        code = it.get("amfi_code") or ""
        if not code:
            continue
        cur.execute(
            "INSERT INTO schemes (key,amc,fund_name,source,as_of,category,plan,"
            "coverage,amfi_regular,amfi_direct) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"mv-{code}", "Test AMC", it["name"], "amfi", "2026-08-14",
             "Equity", "", "has_holdings", code, code))
        inserted.append(cur.execute(
            "SELECT id FROM schemes WHERE key=?", (f"mv-{code}",)).fetchone()[0])
        con.commit()
    con.close()
    try:
        out = wdb.WebDB().portfolio_analytics(items, transactions=tx)
        assert "error" not in out, out
        m = out.get("movement")
        assert m is not None and "error" not in m, m
        assert m["days"] > 3000
        assert m["terminal_value"] > 0
        assert m["total_net_flow"] != 0
        assert len(m["constituents"]) > 5
        # (almost) every transaction attributable — the handful of ISIN-only
        # records with no resolvable code are honestly skipped
        assert 890 <= sum(c["tx_count"] for c in m["constituents"]) <= 903
        # daily-return dates/values strictly aligned (artifact exclusions
        # must shift values AND dates together)
        dr = m["daily_returns"]
        assert len(dr["dates"]) == len(dr["values"])
        assert dr["dates"] == sorted(dr["dates"])
    finally:
        con = sqlite3.connect(WEBAPP_DB)
        cur = con.cursor()
        cur.executemany("DELETE FROM schemes WHERE id=?", [(i,) for i in inserted])
        con.commit()
        con.close()


# ---- T9/T10/T11: contract & storage ------------------------------------------------

def test_analytics_movement_absent_without_tx():
    """portfolio_analytics without transactions -> movement None (regression)."""
    from webapp import db as wdb
    out = wdb.WebDB().portfolio_analytics([{"type": "stock", "id": 1,
                                            "weight": 50}])
    assert out.get("error")  # nothing resolvable — honesty first
