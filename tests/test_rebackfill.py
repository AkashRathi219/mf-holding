"""PLAN_STOCK_DATA_NSE_CLEANUP phase-3: --rebackfill-nse (local-only refill).

Covers merge order + dedupe (bhavcopy authoritative on overlap), raw-scale
preservation (no split adjustment even when the actions file carries splits),
checkpoint write/resume/--force, and the no-wipe-without-replacement safety
policy. Everything runs against tmp_path sandboxes - no network, no real
data/ tree.
"""

from __future__ import annotations

import json
import sys
from datetime import date

import pytest

import src.stock_price as sp

BHAV_HEADER = ("SYMBOL,SERIES,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,"
               "LAST_PRICE,PREVCLOSE,TTL_TRD_QNTY,TURNOVER_LACS\n")

IDENTITY = {
    "INE000TEST01": {"symbol": "TEST", "name": "Test Industries Ltd"},
    "INE111OTHER1": {"symbol": "OTHER", "name": "Other Ltd"},
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    hist = tmp_path / "stock_history"
    bhav = tmp_path / "stock_bhavcopy"
    rawd = tmp_path / "nse_historical"
    acts = tmp_path / "stock_actions"
    for d in (hist, bhav, rawd, acts):
        d.mkdir()
    monkeypatch.setattr(sp, "HISTORY_DIR", hist)
    monkeypatch.setattr(sp, "BHAVCOPY_CACHE_DIR", bhav)
    monkeypatch.setattr(sp, "NSE_HIST_RAW_DIR", rawd)
    monkeypatch.setattr(sp, "REBACKFILL_STATUS", bhav / "nse_backfill_status.json")
    monkeypatch.setattr(sp, "ACTIONS_DIR", acts)
    monkeypatch.setattr(sp, "load_identity", lambda: dict(IDENTITY))
    return {"hist": hist, "bhav": bhav, "raw": rawd, "acts": acts}


def put_bhav(bhav_dir, d: date, rows: dict[str, tuple]):
    """One sec_bhavdata_full-style daily CSV; rows = {symbol: (o, h, low, c, v)}."""
    lines = [BHAV_HEADER]
    for sym, (o, h, low, c, v) in rows.items():
        lines.append(f"{sym},EQ,{o},{h},{low},{c},{c},{c},{v},0.0\n")
    (bhav_dir / (d.strftime("%Y%m%d") + ".csv")).write_text(
        "".join(lines), encoding="latin-1")


def put_dump(raw_dir, symbol: str, points: list[dict]):
    sp.save_json(raw_dir / f"{symbol}.json",
                 {"symbol": symbol, "history": points})


def read_hist(hist_dir, isin: str) -> dict:
    return json.loads((hist_dir / f"{isin}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- merge order
def test_merge_order_dedupe_bhavcopy_authoritative(env):
    # pre-2020 dump segment + one deliberately stale point ON a 2020 date
    put_dump(env["raw"], "TEST", [
        {"date": "27-Dec-2019", "close": 99.0, "open": 98.0,
         "high": 100.0, "low": 97.0, "volume": 10},
        {"date": "02-Jan-2020", "close": 999.0, "open": 999.0,
         "high": 999.0, "low": 999.0, "volume": 10},
    ])
    put_bhav(env["bhav"], date(2020, 1, 2), {"TEST": (1000, 1010, 990, 1000, 500)})
    put_bhav(env["bhav"], date(2020, 1, 3), {"TEST": (1010, 1020, 1005, 1015, 600)})

    res = sp.rebackfill_nse(symbols=["TEST"])
    assert res["ok"] == 1 and res["no_source"] == 0 and res["failed"] == 0

    doc = read_hist(env["hist"], "INE000TEST01")
    pts = doc["history"]
    assert [p["date"] for p in pts] == ["27-Dec-2019", "02-Jan-2020", "03-Jan-2020"]
    by_date = {p["date"]: p for p in pts}
    assert by_date["27-Dec-2019"]["close"] == 99.0      # dump-only date kept
    assert by_date["02-Jan-2020"]["close"] == 1000.0    # bhavcopy wins overlap
    assert by_date["02-Jan-2020"]["volume"] == 500      # full bhav row, not dump stub
    assert by_date["03-Jan-2020"]["close"] == 1015.0    # bhav-only date kept
    assert doc["source"] == "NSE local re-backfill"
    assert doc["currency"] == "INR" and doc["symbol"] == "TEST"


# ------------------------------------------------------------- raw scale only
def test_raw_scale_preserved_no_split_adjustment(env):
    # actions file claims a 2:1 split on 01-Jun-2021: rebackfill must IGNORE it
    sp.save_json(env["acts"] / "INE000TEST01.json",
                 {"splits": [{"date": "01-Jun-2021", "ratio": "2:1"}]})
    put_bhav(env["bhav"], date(2021, 5, 31), {"TEST": (1990, 2000, 1980, 2000, 111)})
    put_bhav(env["bhav"], date(2021, 6, 1), {"TEST": (1000, 1010, 995, 1005, 222)})
    # legacy corrupted doc carried a watermark; the rewrite must drop it
    sp.save_json(env["hist"] / "INE000TEST01.json",
                 {"isin": "INE000TEST01", "source": "Yahoo Finance",
                  "splits_applied_through": "2018-04-30", "history": []})

    sp.rebackfill_nse(symbols=["TEST"])

    doc = read_hist(env["hist"], "INE000TEST01")
    assert "splits_applied_through" not in doc
    assert doc["source"] == "NSE local re-backfill"
    closes = {p["date"]: p["close"] for p in doc["history"]}
    assert closes["31-May-2021"] == 2000.0              # RAW, unscaled
    assert closes["01-Jun-2021"] == 1005.0              # RAW, unscaled


# ------------------------------------------------------ checkpoint / resume
def test_checkpoint_written_and_resume_skips_ok(env):
    put_dump(env["raw"], "OTHER", [{"date": "15-Jan-2019", "close": 50.0}])
    put_bhav(env["bhav"], date(2020, 1, 2),
             {"TEST": (10, 11, 9, 10, 1), "OTHER": (20, 21, 19, 20, 2)})

    first = sp.rebackfill_nse()
    assert first["ok"] == 2 and first["skipped_resume"] == 0

    status = sp.load_json(sp.REBACKFILL_STATUS, {})
    assert status["INE000TEST01"]["status"] == "ok"
    assert status["INE000TEST01"]["points"] == 1
    assert status["INE111OTHER1"]["points"] == 2        # dump + bhav merged
    assert status["INE111OTHER1"]["refilled_at"]

    # resume: ok ISINs skipped even though their files were destroyed meanwhile
    (env["hist"] / "INE000TEST01.json").unlink()
    second = sp.rebackfill_nse()
    assert second["skipped_resume"] == 2 and second["ok"] == 0
    assert not (env["hist"] / "INE000TEST01.json").exists()

    # 'failed' entries are always retried without --force
    status["INE000TEST01"] = {"status": "failed", "points": 0}
    sp.save_json(sp.REBACKFILL_STATUS, status)
    third = sp.rebackfill_nse()
    assert third["ok"] == 1 and third["skipped_resume"] == 1


def test_force_redoes_ok_isins(env):
    put_bhav(env["bhav"], date(2020, 1, 2), {"TEST": (10, 11, 9, 10, 1)})
    assert sp.rebackfill_nse(symbols=["TEST"])["ok"] == 1
    (env["hist"] / "INE000TEST01.json").unlink()

    again = sp.rebackfill_nse(symbols=["TEST"], force=True)
    assert again["ok"] == 1 and again["skipped_resume"] == 0
    assert len(read_hist(env["hist"], "INE000TEST01")["history"]) == 1


# --------------------------------------------- safety: no wipe w/o replacement
def test_no_source_leaves_existing_file_untouched(env):
    sentinel = {"isin": "INE111OTHER1", "source": "legacy",
                "history": [{"date": "03-Jan-2000", "close": 8.5}]}
    sp.save_json(env["hist"] / "INE111OTHER1.json", sentinel)

    res = sp.rebackfill_nse(symbols=["OTHER"])

    assert res["no_source"] == 1 and res["ok"] == 0
    status = sp.load_json(sp.REBACKFILL_STATUS, {})
    assert status["INE111OTHER1"]["status"] == "no_source"
    after = read_hist(env["hist"], "INE111OTHER1")
    assert after == sentinel                            # byte-for-byte untouched

    # resume treats no_source as terminal unless forced
    assert sp.rebackfill_nse()["skipped_resume"] == 1


# ------------------------------------------------- raw-source row selection
def test_bhavcopy_when_issued_series_never_wins(env):
    # HDFC-merger style duplicate rows: a W3 'when-issued' price (different
    # share basis) must not overwrite the real EQ close of the same day.
    (env["bhav"] / "20230717.csv").write_text(
        BHAV_HEADER
        + "TEST,EQ,1644.5,1682.0,1633.0,1678.35,1678.9,1660.51,24626464,408925.08\n"
        + "TEST,W3,557.25,612.95,560.0,612.95,612.95,588.2,66600,391.74\n",
        encoding="latin-1")

    res = sp.rebackfill_nse(symbols=["TEST"])

    pts = read_hist(env["hist"], "INE000TEST01")["history"]
    assert res["ok"] == 1 and len(pts) == 1
    assert pts[0]["close"] == 1678.35 and pts[0]["volume"] == 24626464


# ------------------------------------------------------------------ CLI wiring
def test_cli_rebackfill_flag(env, monkeypatch):
    put_bhav(env["bhav"], date(2020, 1, 2), {"TEST": (10, 11, 9, 10, 1)})
    monkeypatch.setattr(sys, "argv",
                        ["stock_price.py", "--rebackfill-nse", "--symbols", "TEST"])

    assert sp.main() == 0

    doc = read_hist(env["hist"], "INE000TEST01")
    assert doc["source"] == "NSE local re-backfill"
    assert doc["history"][0]["date"] == "02-Jan-2020"
