"""Phase-2 fill tests [PLAN_STOCK_DATA_NSE_CLEANUP]: subject grammar + merge.

Pure-function/parser tests plus tmp_path-sandboxed merge-policy runs — no
network, no real data/ tree (dirs are injected).
"""

from __future__ import annotations

import json

import pytest

import src.stock_actions as sa


def _write_json(path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Subject parser: dividends
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,amount", [
    ("Dividend - Rs 13 Per Share", 13.0),
    ("Dividend - Rs 1.50 Per Share", 1.5),
    ("Special Dividend - Rs 5 Per Share", 5.0),
    (" Interim Dividend Rs.8/- Per Share", 8.0),
    ("Interim Dividend - Re 1 Per Share", 1.0),
    ("Interim Dividend - Re 0.50 Per Share", 0.5),
    ("Annual General Meeting/Dividend - Re 1 Per Share", 1.0),
    ("Agm/Div-Rs.5/- Per Share", 5.0),
    ("Agm/Div-Re0.45 Per Share", 0.45),
    ("Agm/Div-Rs15 Per Share", 15.0),
    ("Annual General Meeting / Final Dividend Rs.8/- Per Ordinary Share", 8.0),
    ("Annual General Meeting /Dividend Re 0.80 Per Equity Share", 0.8),
    ("Agm/Div-Rs.1.25/-Pershare", 1.25),
    ("2nd Int Div-Rs.1.50 P Shrpurpose Revised", 1.5),
    ("Agm/Div-Rs.11.50 Pr Share", 11.5),
    (" Dividend Rs - 9.40 Per Share", 9.4),          # dash-as-separator typo
    ("Annual General Meeting/Dividend Rs:- 6.50/- Per Share", 6.5),
    ("Annual General Meeting/Dividend - Rs 62..50/- Per Share", 62.5),  # '..' typo
    ("Dividend - Rs 0 .70 Per Share", 0.7),          # spaced decimal point
])
def test_parse_dividend_variants(subject, amount):
    ev = sa.parse_subject_events(subject)
    assert {"kind": "dividend", "amount": amount} in ev


@pytest.mark.parametrize("subject", [
    "Agm/Dividend - 100%",                       # percent-only: no absolute Rs
    "Distribution - Rs 2 Per Unit Consists Of Rs 1.70 Per Unit As Interest",
    "Annual General Meeting/Dividend - Rs  Per Share",   # no amount at all
    "Buy Back",
    "Rights Issue",
    "Demerger",
    "",
])
def test_parse_no_dividend(subject):
    assert not [e for e in sa.parse_subject_events(subject) if e["kind"] == "dividend"]


# ---------------------------------------------------------------------------
# Subject parser: bonus ratio semantics (A:B new:old -> price x B/(A+B))
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,ratio", [
    ("Bonus 1:1", "2:1"),
    ("Bonus 2:5", "7:5"),
    ("Bonus 3:1", "4:1"),
    ("Bonus- 1:2", "3:2"),
    ("Bonus 1 : 1", "2:1"),
    ("Bonus 1:10", "11:10"),
    ("Annual General Meeting/Dividend - Rs 2.50 Per Share/Bonus 1:10 (Revised)", None),
])
def test_parse_bonus_ratio_semantics(subject, ratio):
    ev = sa.parse_subject_events(subject)
    splits = [e for e in ev if e["kind"] == "split"]
    assert len(splits) == 1
    if ratio is not None:
        assert splits[0]["ratio"] == ratio


@pytest.mark.parametrize("subject", [
    "Scheme Of Arrangement - Bonus Debentures 6:1",     # not equity bonus
    "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
    "Sch Of Agmt- Bonus Deb1:1",
    "Bonus Ncrps 1:116",
    "Bonus Preference Shares 21:1",
    "Bonus 1 : 1250",                                    # implausible ratio
])
def test_parse_bonus_skips_non_equity(subject):
    assert sa.parse_subject_events(subject) == []


# ---------------------------------------------------------------------------
# Subject parser: FV splits (FV old->new == price x new/old == 'OLD:NEW')
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,ratio", [
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "10:1"),
    ("Face Value Split From Rs.10/- To Rs.2/-", "5:1"),
    ("Stock Split - From Rs 10 To Rs 5", "2:1"),
    ("Face Value Split From Rs 2 To Re 1", "2:1"),
    ("Face Value Split From Rs.5/- To Rs.2/-", "5:2"),
    ("Fv Splt Frm Rs 10 To Rs 2", "5:1"),                # typo keywords
    ("Fv Split Rs.10/- To Rs.2/", "5:1"),                # no from/to keywords
    ("Fv Split Rs.10/- To Re.1/Record Date Revised", "10:1"),
])
def test_parse_fv_split_ratio_derivation(subject, ratio):
    ev = sa.parse_subject_events(subject)
    assert ev == [{"kind": "split", "ratio": ratio}]


def test_parse_mixed_bonus_plus_fvsplit_composes_one_ratio():
    # bonus 1:1 doubles shares, FV 10->5 doubles again => price /4 => '4:1'
    assert sa.parse_subject_events(
        "Bonus 1:1 And Face Value Split Rs.10/- To Rs.5/- Per Share") == [
        {"kind": "split", "ratio": "4:1"}]


def test_parse_dividend_plus_fvsplit_yields_both_events():
    ev = sa.parse_subject_events(
        "Interim Div - Rs 6/- Per Share + Face Value Split (Sub-Division) "
        "- From Rs 10/- Per Share To Re 1/- Per Share")
    assert {"kind": "dividend", "amount": 6.0} in ev
    assert {"kind": "split", "ratio": "10:1"} in ev


def test_parse_dual_dividend_subject_sums_same_exdate_cash():
    # 'Interim X / Special Y' rows declare both for one ex-date -> total
    assert sa.parse_subject_events(
        "Interim Dividend Rs 11 Per Share/ Special Dividend Rs 46 Per Share") == [
        {"kind": "dividend", "amount": 57.0}]
    assert sa.parse_subject_events(
        "Annual General Meeting/Dividend - Rs 15 Per Share/Special Dividend - Rs 5 Per Share") == [
        {"kind": "dividend", "amount": 20.0}]
    # ...but an all-in figure already covers the special part: no double count
    assert sa.parse_subject_events(
        "Dividend-Rs.8.50 Per Share (Including Special Dividend Of Rs.2/- Per Share).") == [
        {"kind": "dividend", "amount": 8.5}]


def test_parse_other_purposes_yield_nothing():
    for subject in ("Buy Back Of Shares", "Scheme Of Demerger", "Interest Payment",
                    "Annual Book Closure", "Extra Ordinary General Meeting"):
        assert sa.parse_subject_events(subject) == []


# ---------------------------------------------------------------------------
# Merge policy (sandboxed to tmp_path; identity injected)
# ---------------------------------------------------------------------------

ISIN = "INE000000001"
IDENT = {ISIN: {"symbol": "TEST", "name": "Test Industries Ltd"}}


class Sandbox:
    def __init__(self, tmp_path):
        self.actions = tmp_path / "stock_actions"
        self.nse_raw = tmp_path / "raw" / "nse_actions"
        self.yahoo_raw = tmp_path / "raw" / "yahoo_actions"
        for d in (self.actions, self.nse_raw, self.yahoo_raw):
            d.mkdir(parents=True)

    def store_path(self):
        return self.actions / f"{ISIN}.json"

    def fill(self, dry_run=False, symbols=None):
        return sa.fill_actions_from_dumps(symbols=symbols, identity=IDENT,
                                          dry_run=dry_run, actions_dir=self.actions,
                                          nse_raw_dir=self.nse_raw,
                                          yahoo_raw_dir=self.yahoo_raw)


@pytest.fixture()
def sb(tmp_path):
    box = Sandbox(tmp_path)
    _write_json(box.store_path(), {
        "isin": ISIN, "symbol": "TEST", "name": "Test Industries Ltd",
        "fetched_at": "2026-01-01T00:00:00",
        "dividends": [{"date": "30-Apr-1996", "amount": 0.37},
                      {"date": "30-Apr-2010", "amount": 1.20},
                      {"date": "14-Aug-2025", "amount": 5.5}],
        "splits": [{"date": "30-Sep-1997", "ratio": "2.0:1.0"},
                   {"date": "30-Sep-2024", "ratio": "2.0:1.0"}],
        "announcements": [{"date": "28-May-2026", "headline": "AGM clippings"}]})
    return box


def _nse_rows(rows):
    return {"symbol": "TEST", "isin": ISIN, "source": sa.NSE_SOURCE,
            "fetched_at": "2026-08-26T00:00:00", "actions": rows}


DEEP_DIVIDENDS = [{"date": "30-Apr-1996", "amount": 0.37},
                  {"date": "30-Apr-2010", "amount": 1.20}]
DEEP_SPLITS = [{"date": "30-Sep-1997", "ratio": "2.0:1.0"}]


def test_fill_preserves_deep_history_and_overrides_nse_window(sb):
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Dividend - Rs 5.75 Per Share", "exDate": "14-Aug-2025"},
        {"subject": "Dividend - Rs 6 Per Share", "exDate": "31-May-2026"},
        {"subject": "Bonus 1:10", "exDate": "28-Oct-2024"},   # new event: 11:10
        {"subject": "Demerger", "exDate": "20-Jul-2023"},     # skipped purpose
    ]))
    summary = sb.fill()
    assert summary["processed"] == 1 and summary["skipped"] == 0
    assert summary["dividends_updated"] == 1      # 14-Aug-2025: 5.5 -> 5.75
    assert summary["dividends_added"] == 1        # 31-May-2026
    assert summary["splits_added"] == 1           # bonus as split-equivalent
    assert summary["splits_updated"] == 0 and summary["yahoo_used"] == 0
    assert summary["aborted_shrink"] == 0
    doc = _read_json(sb.store_path())
    divs = {d["date"]: d["amount"] for d in doc["dividends"]}
    # deep history intact...
    for old in DEEP_DIVIDENDS:
        assert divs[old["date"]] == old["amount"]
    assert doc["dividends"][0] == DEEP_DIVIDENDS[0]       # oldest entry untouched
    ratios = {s["date"]: s["ratio"] for s in doc["splits"]}
    for old in DEEP_SPLITS:
        assert ratios[old["date"]] == old["ratio"]
    # ...NSE window overridden/added
    assert divs["14-Aug-2025"] == 5.75 and divs["31-May-2026"] == 6.0
    assert ratios["28-Oct-2024"] == "11:10"
    # untouched fields + additive source note
    assert doc["announcements"] == [{"date": "28-May-2026", "headline": "AGM clippings"}]
    assert doc["sources"] == [sa.NSE_SOURCE]
    assert doc["fetched_at"] != "2026-01-01T00:00:00"


def test_fill_skipped_when_dump_missing_or_eventless(sb):
    before = sb.store_path().read_text(encoding="utf-8")
    # symbol with a dump that only carries non-action rows -> nothing usable
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Annual General Meeting", "exDate": "05-Jun-2026"},
        {"subject": "Buy Back", "exDate": "01-Jul-2026"}]))
    summary = sb.fill()
    assert summary["skipped"] == 1 and summary["processed"] == 1
    assert summary["dividends_added"] == 0 and summary["splits_added"] == 0
    assert sb.store_path().read_text(encoding="utf-8") == before   # untouched
    # and with no dump file at all
    (sb.nse_raw / "TEST.json").unlink()
    assert sb.fill()["skipped"] == 1
    assert sb.store_path().read_text(encoding="utf-8") == before


def test_fill_yahoo_fallback_only_when_nse_window_empty(tmp_path):
    box = Sandbox(tmp_path)
    ident = {"INE000000001": {"symbol": "WITHNSE", "name": "A Ltd"},
             "INE000000002": {"symbol": "NOEVENTS", "name": "B Ltd"}}
    _write_json(box.nse_raw / "WITHNSE.json", _nse_rows(
        [{"subject": "Dividend - Rs 2 Per Share", "exDate": "05-Jun-2026"}]))
    _write_json(box.nse_raw / "NOEVENTS.json", _nse_rows([]))
    yahoo_a = {"symbol": "WITHNSE", "dividends": [{"date": "10-Jan-2020", "amount": 9.9}],
               "splits": []}
    yahoo_b = {"symbol": "NOEVENTS", "dividends": [{"date": "15-Mar-2024", "amount": 1.25}],
               "splits": [{"date": "01-Apr-2019", "ratio": "2.0:1.0"}]}
    _write_json(box.yahoo_raw / "WITHNSE.json", yahoo_a)
    _write_json(box.yahoo_raw / "NOEVENTS.json", yahoo_b)
    summary = sa.fill_actions_from_dumps(identity=ident, actions_dir=box.actions,
                                         nse_raw_dir=box.nse_raw,
                                         yahoo_raw_dir=box.yahoo_raw)
    assert summary["yahoo_used"] == 1              # only NOEVENTS fell back
    assert summary["skipped"] == 0
    doc_a = _read_json(box.actions / "INE000000001.json")
    assert [d["date"] for d in doc_a["dividends"]] == ["05-Jun-2026"]  # yahoo ignored
    assert doc_a["sources"] == [sa.NSE_SOURCE]
    doc_b = _read_json(box.actions / "INE000000002.json")
    assert [(d["date"], d["amount"]) for d in doc_b["dividends"]] == [("15-Mar-2024", 1.25)]
    assert [(s["date"], s["ratio"]) for s in doc_b["splits"]] == [("01-Apr-2019", "2:1")]
    assert doc_b["sources"] == [sa.NSE_SOURCE, sa.YAHOO_FALLBACK_SOURCE]


def test_fill_is_idempotent_second_run_changes_nothing(sb):
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Dividend - Rs 6 Per Share", "exDate": "31-May-2026"},
        {"subject": "Dividend - Rs 5.75 Per Share", "exDate": "14-Aug-2025"}]))
    first = sb.fill()
    assert first["dividends_added"] == 1 and first["dividends_updated"] == 1
    snapshot = sb.store_path().read_text(encoding="utf-8")
    second = sb.fill()
    assert second["dividends_added"] == 0 and second["dividends_updated"] == 0
    assert second["splits_added"] == 0 and second["splits_updated"] == 0
    assert sb.store_path().read_text(encoding="utf-8") == snapshot  # byte-identical


def test_fill_dry_run_reports_without_writing(sb):
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Dividend - Rs 6 Per Share", "exDate": "31-May-2026"}]))
    before = sb.store_path().read_text(encoding="utf-8")
    summary = sb.fill(dry_run=True)
    assert summary["dividends_added"] == 1
    assert sb.store_path().read_text(encoding="utf-8") == before


def test_fill_aborts_shrink_and_leaves_store_untouched(sb, monkeypatch):
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Dividend - Rs 6 Per Share", "exDate": "31-May-2026"}]))
    orig_merge = sa._merge_kind

    def shrinking_merge(existing, overrides, field):
        merged, added, updated = orig_merge(existing, overrides, field)
        return merged[:1], added, updated       # simulate a history-destroying bug

    monkeypatch.setattr(sa, "_merge_kind", shrinking_merge)
    before = sb.store_path().read_text(encoding="utf-8")
    summary = sb.fill()
    assert summary["aborted_shrink"] == 1
    assert sb.store_path().read_text(encoding="utf-8") == before


def test_fill_skips_split_rereported_under_other_feed_date(sb):
    # Yahoo already holds the 2017 event dated 31-Aug; NSE re-reports the same
    # 2:1 bonus with ex-date 07-Sep -> adding both would double-adjust prices.
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Bonus 1:1", "exDate": "07-Sep-1997"}]))
    summary = sb.fill()
    assert summary["splits_added"] == 0
    doc = _read_json(sb.store_path())
    assert [(s["date"], s["ratio"]) for s in doc["splits"]] == [
        ("30-Sep-1997", "2.0:1.0"), ("30-Sep-2024", "2.0:1.0")]
    # ...while a genuinely new ratio/date still lands
    _write_json(sb.nse_raw / "TEST.json", _nse_rows([
        {"subject": "Bonus 1:1", "exDate": "07-Sep-1997"},
        {"subject": "Bonus 1:2", "exDate": "10-Oct-1997"}]))
    assert sb.fill()["splits_added"] == 1


def test_fill_symbols_filter_targets_subset(sb):
    ident = dict(IDENT)
    ident["INE000000009"] = {"symbol": "OTHER", "name": "Other Ltd"}
    _write_json(sb.nse_raw / "OTHER.json", _nse_rows(
        [{"subject": "Dividend - Rs 1 Per Share", "exDate": "01-Feb-2026"}]))
    summary = sa.fill_actions_from_dumps(symbols=["other"], identity=ident,
                                         actions_dir=sb.actions,
                                         nse_raw_dir=sb.nse_raw,
                                         yahoo_raw_dir=sb.yahoo_raw)
    assert summary["processed"] == 1 and summary["dividends_added"] == 1
    assert not (sb.actions / "INE000000001.json").exists() or \
        _read_json(sb.actions / "INE000000001.json").get("fetched_at")
