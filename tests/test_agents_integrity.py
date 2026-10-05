"""[T20] Integrity auditor tests (SPEC §11.1 / AC-13, AC-14, AC-15, AC-21).

Every test builds a tiny synthetic webapp SQLite DB in ``tmp_path`` plus a
parsed-store root seeded from ``tests/fixtures/integrity/*.json`` (real merge
shapes), then runs :func:`src.agents.integrity.run_audit` against sandbox
paths — the real ``data/webapp.db``, ``data/reports``, ``data/reference`` and
``data/logs`` are never touched.

Covers the AC-14 ladder end-to-end through the auditor (full-disclosure 97%
-> ``COMPLETE_100`` not enqueued; truncated top-10 factsheet at 45% ->
``TOP10_FALLBACK`` NEVER enqueued; full-disclosure 60% -> ``ESCALATE``
enqueued exactly once with re-audit dedupe; Σ>105 / weight>100 ->
``SUSPECT_SCALE``), the ``MISSING`` zero-holdings case, the empty-``as_of``
unkeyable bucket, document-class recovery from the parsed stores (source
preference, strict ``unknown`` rule, merge skip-rule mirroring, path-month
fallback), report/sidecar writing and coverage, the ``--limit-amc`` scoped
merge, ``--no-enqueue``, the CLI entrypoint, and the read-only guarantee
(DB file stat unchanged; writes rejected on the read-only URI).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.agents.escalation import EscalationQueue
from src.agents.integrity import (
    UNKNOWN_MONTH,
    _month_from_date,
    main,
    run_audit,
    ParsedIndex,
)
from src.agents.tiers import FLAG_MISSING, FLAG_SUSPECT_SCALE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "integrity"
AMC = "Test AMC Mutual Fund"
AUDIT_DATE = date(2026, 10, 4)

TOP10_45 = [10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5]


def _world_schemes() -> list[dict]:
    return [
        {"amc": AMC, "fund_name": "Test Full Disclosure Fund", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [50.0, 30.0, 17.0]},
        {"amc": AMC, "fund_name": "Test Top Ten Fund", "source": "advisorkhoj",
         "as_of": "2026-07-31", "holdings": list(TOP10_45)},
        {"amc": AMC, "fund_name": "Test Partial Fund", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [30.0, 20.0, 10.0]},
        {"amc": AMC, "fund_name": "Test Suspect Fund", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [40.0, 30.0, 25.0, 22.0]},
        {"amc": AMC, "fund_name": "Test Heavy Weight Fund", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [140.0, -45.0]},
        {"amc": AMC, "fund_name": "Test Empty Fund", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": []},
        {"amc": AMC, "fund_name": "Test No Date Fund", "source": "amc_website",
         "as_of": "", "holdings": [30.0, 20.0, 10.0]},
        {"amc": AMC, "fund_name": "Test Universe Only Fund", "source": "universe_only",
         "as_of": "2026-08-14", "holdings": []},
    ]


def _make_db(path: Path, schemes: list[dict]) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schemes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, amc TEXT, "
                "fund_name TEXT, source TEXT, as_of TEXT)")
    con.execute("CREATE TABLE holdings ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, scheme_id INTEGER, percent_nav REAL)")
    for i, s in enumerate(schemes, start=1):
        con.execute(
            "INSERT INTO schemes (id, key, amc, fund_name, source, as_of) VALUES (?,?,?,?,?,?)",
            (i, f"k{i}", s["amc"], s["fund_name"], s.get("source", "amc_website"),
             s.get("as_of", "")),
        )
        for pct in s.get("holdings", []):
            con.execute("INSERT INTO holdings (scheme_id, percent_nav) VALUES (?,?)", (i, pct))
    con.commit()
    con.close()


def _seed_parsed_root(tmp_path: Path) -> Path:
    root = tmp_path / "parsed"
    monthly_dir = root / "amc_websites" / "Test_AMC" / "2026" / "07"
    monthly_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "amc_website_monthly_portfolio.json",
                monthly_dir / "Monthly_Portfolio_July_2026.json")
    ak_dir = root / "advisorkhoj"
    ak_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "advisorkhoj_monthly_portfolio.json", ak_dir / "ak_monthly.json")
    shutil.copy(FIXTURES / "advisorkhoj_factsheet_top10.json", ak_dir / "ak_factsheet.json")
    return root


def _run(
    tmp_path: Path,
    schemes: list[dict] | None = None,
    *,
    enqueue: bool = True,
    limit_amc: str | None = None,
    parsed: Path | None = None,
) -> dict:
    db = tmp_path / "webapp.db"
    _make_db(db, schemes if schemes is not None else _world_schemes())
    parsed_root = parsed if parsed is not None else _seed_parsed_root(tmp_path)
    return run_audit(
        db,
        parsed_root,
        report_dir=tmp_path / "reports",
        tiers_out=tmp_path / "reference" / "integrity_tiers.json",
        queue_path=tmp_path / "logs" / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "reference" / "manual_intervention.csv",
        enqueue=enqueue,
        limit_amc=limit_amc,
        audit_date=AUDIT_DATE,
    )


def _sidecar(tmp_path: Path) -> dict:
    path = tmp_path / "reference" / "integrity_tiers.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _tickets(tmp_path: Path) -> dict:
    queue = EscalationQueue(
        queue_path=tmp_path / "logs" / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "reference" / "manual_intervention.csv",
    )
    return queue.load()


def _queue_lines_for(tmp_path: Path, scheme: str) -> list[dict]:
    path = tmp_path / "logs" / "escalation_queue.jsonl"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("scheme") == scheme:
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# AC-14 (a): full-disclosure 97% -> COMPLETE_100, NOT enqueued
# ---------------------------------------------------------------------------

def test_full_disclosure_97pct_is_t0_and_not_enqueued(tmp_path):
    _run(tmp_path)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Full Disclosure Fund|2026-07"]
    assert entry["tier"] == "COMPLETE_100"
    assert entry["tier_code"] == "T0"
    assert entry["document_class"] == "full_portfolio"
    assert entry["coverage_pct"] == 97.0
    assert entry["n_holdings"] == 3
    assert entry["escalate"] is False
    assert entry["flags"] == []
    assert all(t.scheme != "Test Full Disclosure Fund" for t in _tickets(tmp_path).values())


# ---------------------------------------------------------------------------
# AC-14 (b): truncated top-10 factsheet at 45% -> TOP10_FALLBACK, NEVER enqueued
# ---------------------------------------------------------------------------

def test_factsheet_top10_45pct_is_t1_and_never_enqueued(tmp_path):
    _run(tmp_path)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Top Ten Fund|2026-07"]
    assert entry["tier"] == "TOP10_FALLBACK"
    assert entry["tier_code"] == "T1"
    assert entry["document_class"] == "factsheet_topn"
    assert entry["coverage_pct"] == 45.0
    assert entry["n_holdings"] == 10
    assert entry["escalate"] is False
    assert all(t.scheme != "Test Top Ten Fund" for t in _tickets(tmp_path).values())


# ---------------------------------------------------------------------------
# AC-14 (c) + AC-15: full-disclosure 60% -> ESCALATE, enqueued exactly once
# ---------------------------------------------------------------------------

def test_full_disclosure_60pct_is_t2_enqueued_exactly_once(tmp_path):
    _run(tmp_path)
    tickets = _tickets(tmp_path)
    key = (AMC, "Test Partial Fund", "2026-07")
    assert key in tickets
    ticket = tickets[key]
    assert ticket.status == "OPEN"
    assert ticket.tier == "T2"
    assert ticket.coverage_pct == 60.0
    assert ticket.document_class == "full_portfolio"
    assert ticket.channels_tried == []
    assert len(_queue_lines_for(tmp_path, "Test Partial Fund")) == 1
    _run(tmp_path)
    assert len(_queue_lines_for(tmp_path, "Test Partial Fund")) == 1
    assert _tickets(tmp_path)[key].status == "OPEN"


# ---------------------------------------------------------------------------
# AC-14 (d): SUSPECT_SCALE (Σ>105 or a single weight>100)
# ---------------------------------------------------------------------------

def test_sum_over_105_is_suspect_scale_and_enqueued(tmp_path):
    _run(tmp_path)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Suspect Fund|2026-07"]
    assert FLAG_SUSPECT_SCALE in entry["flags"]
    assert entry["tier_code"] == "T2"
    assert entry["escalate"] is True
    assert entry["coverage_pct"] == 117.0
    assert (AMC, "Test Suspect Fund", "2026-07") in _tickets(tmp_path)


def test_single_weight_over_100_flagged_and_escalate_true(tmp_path):
    report = _run(tmp_path)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Heavy Weight Fund|2026-07"]
    assert FLAG_SUSPECT_SCALE in entry["flags"]
    assert entry["tier"] == "COMPLETE_100"
    assert entry["tier_code"] == "T0"
    assert entry["escalate"] is True
    assert all(t.scheme != "Test Heavy Weight Fund" for t in _tickets(tmp_path).values())
    assert report["escalations_blocked_non_t2"] == 1


# ---------------------------------------------------------------------------
# MISSING: zero holding rows -> T2 + escalated
# ---------------------------------------------------------------------------

def test_zero_holdings_is_missing_t2_and_enqueued(tmp_path):
    _run(tmp_path)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Empty Fund|2026-07"]
    assert FLAG_MISSING in entry["flags"]
    assert entry["tier_code"] == "T2"
    assert entry["escalate"] is True
    assert entry["coverage_pct"] == 0.0
    assert entry["n_holdings"] == 0
    assert (AMC, "Test Empty Fund", "2026-07") in _tickets(tmp_path)


# ---------------------------------------------------------------------------
# empty as_of -> unkeyable bucket, no crash, still audited
# ---------------------------------------------------------------------------

def test_empty_as_of_lands_in_unkeyable_without_crash(tmp_path):
    report = _run(tmp_path)
    assert report["unkeyable_schemes"] == 1
    entry = _sidecar(tmp_path)[f"{AMC}|Test No Date Fund|{UNKNOWN_MONTH}"]
    assert entry["tier_code"] == "T2"
    assert entry["document_class"] == "unknown"
    assert (AMC, "Test No Date Fund", UNKNOWN_MONTH) in _tickets(tmp_path)


# ---------------------------------------------------------------------------
# reports + sidecar written; sidecar covers every audited scheme
# ---------------------------------------------------------------------------

def test_reports_and_sidecar_written_covering_every_scheme(tmp_path):
    schemes = _world_schemes()
    _run(tmp_path)
    report_path = tmp_path / "reports" / "integrity_2026-10-04.json"
    md_path = tmp_path / "reports" / "integrity_2026-10-04.md"
    assert report_path.exists() and md_path.exists()
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["tier_histogram"] == {"T0": 2, "T1": 1, "T2": 5, "T3": 0}
    assert sum(written["tier_histogram"].values()) == written["n_schemes"] == len(schemes)
    assert written["escalations_enqueued"] == 5
    assert written["escalate_true_schemes"] == 6
    assert written["flags"] == {"MISSING": 2, "SUSPECT_SCALE": 2}
    assert written["unkeyable_schemes"] == 1
    assert written["universe_only_schemes"] == 1
    assert written["document_classes"] == {
        "full_portfolio": 3, "factsheet_topn": 1, "unknown": 4,
    }
    sidecar = _sidecar(tmp_path)
    assert len(sidecar) == len(schemes)
    for s in schemes:
        month = s["as_of"][:7] if s["as_of"] else UNKNOWN_MONTH
        assert f"{s['amc']}|{s['fund_name']}|{month}" in sidecar
    for entry in sidecar.values():
        assert set(entry) == {
            "tier", "tier_code", "document_class", "coverage_pct", "n_holdings",
            "escalate", "flags", "reason", "source", "as_of",
        }
    md = md_path.read_text(encoding="utf-8")
    assert "Tier histogram" in md
    assert "Per-AMC breakdown" in md
    assert "Worst offenders per AMC" in md


# ---------------------------------------------------------------------------
# read-only guarantee: the auditor never writes the DB
# ---------------------------------------------------------------------------

def test_auditor_never_writes_the_db(tmp_path):
    db = tmp_path / "webapp.db"
    _make_db(db, _world_schemes())
    parsed = _seed_parsed_root(tmp_path)
    before = db.stat()
    run_audit(
        db,
        parsed,
        report_dir=tmp_path / "reports",
        tiers_out=tmp_path / "reference" / "integrity_tiers.json",
        queue_path=tmp_path / "logs" / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "reference" / "manual_intervention.csv",
        audit_date=AUDIT_DATE,
    )
    after = db.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    ro = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO schemes (key, amc) VALUES ('x', 'y')")
    ro.close()


# ---------------------------------------------------------------------------
# document_class recovery: source preference + strict unknown rule
# ---------------------------------------------------------------------------

def _seed_dual_source(tmp_path: Path) -> Path:
    """Same (amc, fund, month) indexed from BOTH stores with different classes."""
    root = tmp_path / "parsed"
    aw_dir = root / "amc_websites" / "Test_AMC" / "2026" / "07"
    aw_dir.mkdir(parents=True)
    (aw_dir / "Monthly_Portfolio_July_2026.json").write_text(json.dumps({
        "amc_name": AMC,
        "schemes": {"D1": {"fund_name": "Test Dual Fund", "date": "31 Jul 2026",
                           "holdings": [{"company": "X", "percent_nav": 97.0}]}},
    }), encoding="utf-8")
    ak_dir = root / "advisorkhoj"
    ak_dir.mkdir(parents=True)
    (ak_dir / "ak_factsheet.json").write_text(json.dumps({
        "amc": AMC,
        "files": [{"file": "Test AMC Mutual Fund\\07-2026\\factsheet\\Test_Factsheet_July_2026.xls",
                   "status": "ok",
                   "sheets": [{"scheme": "Test Dual Fund", "date": "31 Jul 2026",
                               "plans": {"Direct": {"holdings": [
                                   {"name": "X", "pct_nav": 4.5}]}}}]},
                  ],
    }), encoding="utf-8")
    return root


def test_document_class_prefers_the_displayed_source(tmp_path):
    parsed = _seed_dual_source(tmp_path)
    schemes = [{"amc": AMC, "fund_name": "Test Dual Fund", "source": "advisorkhoj",
                "as_of": "2026-07-31", "holdings": list(TOP10_45)}]
    _run(tmp_path, schemes=schemes, parsed=parsed)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Dual Fund|2026-07"]
    assert entry["source"] == "advisorkhoj"
    assert entry["document_class"] == "factsheet_topn"
    assert entry["tier"] == "TOP10_FALLBACK"
    assert entry["tier_code"] == "T1"
    assert entry["escalate"] is False


def test_document_class_source_preference_other_direction(tmp_path):
    parsed = _seed_dual_source(tmp_path)
    schemes = [{"amc": AMC, "fund_name": "Test Dual Fund", "source": "amc_website",
                "as_of": "2026-07-31", "holdings": [50.0, 30.0, 17.0]}]
    _run(tmp_path, schemes=schemes, parsed=parsed)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Dual Fund|2026-07"]
    assert entry["source"] == "amc_website"
    assert entry["document_class"] == "full_portfolio"
    assert entry["tier"] == "COMPLETE_100"
    assert entry["tier_code"] == "T0"
    assert entry["escalate"] is False


def test_parsed_index_lookup_prefers_matching_source(tmp_path):
    parsed = _seed_dual_source(tmp_path)
    index = ParsedIndex.build(parsed)
    assert index.lookup(AMC, "Test Dual Fund", "2026-07", "amc_website") == "full_portfolio"
    assert index.lookup(AMC, "Test Dual Fund", "2026-07", "advisorkhoj") == "factsheet_topn"
    assert index.lookup(AMC, "Test Dual Fund", "2026-07", "amfi") == "unknown"
    assert index.lookup(AMC, "Test Missing Fund", "2026-07", "amc_website") == "unknown"


def test_unmatched_parsed_store_is_unknown_strict_never_t1(tmp_path):
    schemes = [{"amc": AMC, "fund_name": "Test Orphan Fund", "source": "amc_website",
                "as_of": "2026-07-31", "holdings": list(TOP10_45)}]
    _run(tmp_path, schemes=schemes)
    entry = _sidecar(tmp_path)[f"{AMC}|Test Orphan Fund|2026-07"]
    assert entry["document_class"] == "unknown"
    assert entry["tier_code"] == "T2"
    assert entry["escalate"] is True


def test_path_month_fallback_when_payload_date_unparseable(tmp_path):
    schemes = [{"amc": AMC, "fund_name": "Test No Date Fund", "source": "amc_website",
                "as_of": "2026-07-31", "holdings": [50.0, 30.0, 17.0]}]
    _run(tmp_path, schemes=schemes)
    entry = _sidecar(tmp_path)[f"{AMC}|Test No Date Fund|2026-07"]
    assert entry["document_class"] == "full_portfolio"
    assert entry["tier"] == "COMPLETE_100"


def test_parsed_index_mirrors_merge_skip_rules(tmp_path):
    root = tmp_path / "parsed"
    amc_dir = root / "amc_websites" / "Test_AMC" / "2026" / "07"
    amc_dir.mkdir(parents=True)
    good = {"amc_name": AMC, "schemes": {
        "G1": {"fund_name": "Test Good Fund", "date": "31 Jul 2026",
               "holdings": [{"company": "X", "percent_nav": 97.0}]}}}
    factsheet = {"amc_name": AMC, "metadata": {}, "schemes": {
        "F1": {"fund_name": "Test Factsheet Fund", "date": "31 Jul 2026",
               "holdings": [{"company": "X", "percent_nav": 45.0}]}}}
    grouped = {"amc_name": AMC, "metadata": {"grouped_factsheet": True}, "schemes": {
        "G2": {"fund_name": "Test Grouped Fund", "date": "31 Jul 2026",
               "holdings": [{"company": "X", "percent_nav": 45.0}]}}}
    (amc_dir / "Monthly_Portfolio_July_2026.json").write_text(json.dumps(good), encoding="utf-8")
    (amc_dir / "Test_AMC_Factsheet_July_2026.json").write_text(json.dumps(factsheet), encoding="utf-8")
    (amc_dir / "Test_AMC_Grouped_July_2026.json").write_text(json.dumps(grouped), encoding="utf-8")
    (amc_dir / "report_2026_07.json").write_text(json.dumps(good), encoding="utf-8")
    index = ParsedIndex.build(root)
    assert index.lookup(AMC, "Test Good Fund", "2026-07", "amc_website") == "full_portfolio"
    assert index.lookup(AMC, "Test Factsheet Fund", "2026-07", "amc_website") == "unknown"
    assert index.lookup(AMC, "Test Grouped Fund", "2026-07", "amc_website") == "unknown"
    assert index.files_scanned == 4
    assert index.files_indexed == 1


# ---------------------------------------------------------------------------
# universe_only counting
# ---------------------------------------------------------------------------

def test_universe_only_schemes_counted_and_missing(tmp_path):
    report = _run(tmp_path)
    assert report["universe_only_schemes"] == 1
    entry = _sidecar(tmp_path)[f"{AMC}|Test Universe Only Fund|2026-08"]
    assert FLAG_MISSING in entry["flags"]
    assert entry["tier_code"] == "T2"
    assert entry["source"] == "universe_only"


# ---------------------------------------------------------------------------
# scoped run (--limit-amc) + sidecar merge
# ---------------------------------------------------------------------------

def test_limit_amc_scopes_the_run_and_merges_the_sidecar(tmp_path):
    schemes = [
        {"amc": "AMC A", "fund_name": "Fund A1", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [50.0, 30.0, 17.0]},
        {"amc": "AMC B", "fund_name": "Fund B1", "source": "amc_website",
         "as_of": "2026-07-31", "holdings": [30.0, 20.0, 10.0]},
    ]
    _run(tmp_path, schemes=schemes)
    assert len(_sidecar(tmp_path)) == 2
    report = _run(tmp_path, schemes=schemes, limit_amc="amc a")
    assert report["n_schemes"] == 1
    assert report["scope_amc"] == "amc a"
    sidecar = _sidecar(tmp_path)
    assert len(sidecar) == 2
    assert "AMC A|Fund A1|2026-07" in sidecar
    assert "AMC B|Fund B1|2026-07" in sidecar


# ---------------------------------------------------------------------------
# --no-enqueue: audit only, queue untouched
# ---------------------------------------------------------------------------

def test_no_enqueue_writes_no_queue_file(tmp_path):
    report = _run(tmp_path, enqueue=False)
    assert report["enqueue"] is False
    assert report["escalations_enqueued"] == 0
    assert not (tmp_path / "logs" / "escalation_queue.jsonl").exists()
    entry = _sidecar(tmp_path)[f"{AMC}|Test Partial Fund|2026-07"]
    assert entry["tier_code"] == "T2"
    assert entry["escalate"] is True


# ---------------------------------------------------------------------------
# month extraction from parsed date texts
# ---------------------------------------------------------------------------

def test_month_from_date_formats():
    assert _month_from_date("Monthly Portfolio Statement as on July 31, 2026") == "2026-07"
    assert _month_from_date("30 Jun 2026") == "2026-06"
    assert _month_from_date("Top 10 Portfolio Holding as on July 31st 2026") == "2026-07"
    assert _month_from_date("2026-07-31") == "2026-07"
    assert _month_from_date("31-JUL-2026") == "2026-07"
    assert _month_from_date("Holding Statement as on 15.06.2026") == "2026-06"
    assert _month_from_date("August 31,2018") == "2018-08"
    assert _month_from_date("Factsheet July 2026") == "2026-07"
    assert _month_from_date("") == ""
    assert _month_from_date("IDF255") == ""
    assert _month_from_date(None) == ""


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def test_cli_audit_writes_outputs(tmp_path):
    db = tmp_path / "webapp.db"
    _make_db(db, _world_schemes())
    parsed = _seed_parsed_root(tmp_path)
    rc = main([
        "--audit",
        "--db", str(db),
        "--parsed-root", str(parsed),
        "--report-dir", str(tmp_path / "reports"),
        "--tiers-out", str(tmp_path / "reference" / "integrity_tiers.json"),
        "--queue", str(tmp_path / "logs" / "escalation_queue.jsonl"),
    ])
    assert rc == 0
    reports = list((tmp_path / "reports").glob("integrity_*.json"))
    mds = list((tmp_path / "reports").glob("integrity_*.md"))
    assert len(reports) == 1 and len(mds) == 1
    assert (tmp_path / "reference" / "integrity_tiers.json").exists()
    assert len(_sidecar(tmp_path)) == len(_world_schemes())


def test_cli_requires_audit_flag(capsys):
    assert main([]) == 2
    assert "--audit" in capsys.readouterr().out
