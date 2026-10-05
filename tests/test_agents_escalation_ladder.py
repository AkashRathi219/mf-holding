"""End-to-end escalation-ladder integration test (SPEC §11.0-§11.4, T19/T20/T32-T35).

ONE test drives the WHOLE §11 ladder across the modules that exist today -
``src.agents.integrity.run_audit`` (audit) -> ``EscalationQueue`` (tickets) ->
``build_dispatcher`` + the real ``amc_recheck`` / ``web_search`` channel
modules (channels in the fixed §11.3 order) -> ``record_attempt`` / the
``N_max`` cap -> ``manual_register`` (close/park) -> a re-audit after the
fill - against a synthetic webapp SQLite DB (schema from ``webapp.db``) and a
synthetic parsed store, everything under ``tmp_path``.

Scenario (four seeded scheme-months, one AMC, month 2026-07):

1. full-disclosure Σ=97  -> T0 ``COMPLETE_100``, never enqueued;
2. factsheet top-10 Σ=45 (``factsheet_topn``) -> T1 ``TOP10_FALLBACK``, NEVER
   enqueued (AC-15: the queue's T2-only gate);
3. full-disclosure Σ=60  -> T2 ``ESCALATE``, enqueued - closed via the channels;
4. full-disclosure Σ=130 -> T2 + ``SUSPECT_SCALE``, enqueued - parked MANUAL
   at the ``N_max`` cap.

Assertions:

A. the audit writes the reports + the AC-21 tier sidecar and enqueues
   EXACTLY the two T2 tickets; the T1 scheme is absent from the queue;
B. channel ORDER: the dispatcher drives ``amc_recheck`` (fails: no candidate
   documents at any alternate location), then ``web_search`` (proposes ONLY a
   NON-allow-listed URL - ``skipped_untrusted_host``, the fetcher is NEVER
   called), then a channel that returns complete T0 holdings closes the
ticket.  The §11.3 slots after ``web_search`` (``amfi``/``advisorkhoj``)
    are registered in the dispatcher; with no HTTP client injected they
    short-circuit via ``skipped_no_client`` (asserted on the parked ticket),
    so the closing T0 evidence arrives through a
   ``web_search`` re-probe whose provider now resolves an ALLOW-LISTED AMFI
   URL whose parse is a complete ``full_portfolio`` (Σ=97) -
   ``record_attempt(..., success=True)`` closes the ticket;
C. the other ticket, driven through failing channels, reaches the ``N_max``
   cap and becomes MANUAL with one register row;
   ``manual_register.for_queue_id`` finds it and it is still pending (no
   auto-close);
D. re-audit after the fill: the closed scheme is now T0 and the re-audit
   adds NO duplicate tickets (the queue file does not grow; the CLOSED and
   MANUAL tickets keep their queue_ids);
E. ALL paths point at tmp dirs - the real ``data/webapp.db``,
   ``data/logs/escalation_queue.jsonl`` and ``data/reference/*.csv`` are
   neither created nor modified (before/after existence+stat snapshot).

Zero network: every channel runs on injected fakes only; the walk helper
mirrors ``Agent._work_escalations`` exactly (``next_channel`` ->
``channel_runner(channel, ticket, failed_strategy=None)`` ->
``queue.record_attempt``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.agents.channels.web_search import plan as plan_queries
from src.agents.dispatch import build_dispatcher
from src.agents.escalation import (
    CHANNELS,
    N_MAX,
    STATUS_CLOSED,
    STATUS_MANUAL,
    STATUS_OPEN,
    EscalationQueue,
    next_channel,
)
from src.agents.integrity import run_audit
from src.agents.manual_register import for_queue_id, pending_rows
from src.agents.taxonomy import is_valid_code

AMC = "Ladder AMC"
MONTH = "2026-07"
AS_OF = "2026-07-31"
FUND_T0 = "Ladder Alpha Fund"
FUND_T1 = "Ladder Bravo Fund"
FUND_CLOSED = "Ladder Charlie Fund"
FUND_SUSPECT = "Ladder Delta Fund"
AUDIT_DATE = date(2026, 10, 5)
REAUDIT_DATE = date(2026, 10, 6)

TOP10_45 = (10.0, 9.0, 8.0, 5.0, 4.0, 3.0, 3.0, 1.5, 1.0, 0.5)
FULL_97 = (50.0, 30.0, 17.0)
PARTIAL_60 = (30.0, 20.0, 10.0)
SUSPECT_130 = (70.0, 60.0)

UNTRUSTED_URL = "https://evil.example/monthly_portfolio_July_2026.xlsx"
TRUSTED_URL = "https://www.amfiindia.com/modules/PortDown.jsp?mf=ladder"

CODE_MISSING = "ERR_SCHEME_MISSING_IN_DB"
CODE_INCOMPLETE_SUM = "ERR_HOLDINGS_INCOMPLETE_SUM"
CODE_MISSING_IN_DB = "ERR_SCHEME_MISSING_IN_DB"

# (fund_name, source, weights) - the four seeded scheme-months, in DB id order.
SCHEMES = (
    (FUND_T0, "amc_website", FULL_97),
    (FUND_T1, "advisorkhoj", TOP10_45),
    (FUND_CLOSED, "amc_website", PARTIAL_60),
    (FUND_SUSPECT, "amc_website", SUSPECT_130),
)


# ---------------------------------------------------------------------------
# Sandbox: synthetic webapp DB (webapp/db.py schema) + parsed stores
# ---------------------------------------------------------------------------

def _make_db(path: Path) -> dict[str, int]:
    """Synthetic webapp DB mirroring ``webapp/db.py``'s schema; returns fund -> id."""
    from webapp.db import _create_schema

    con = sqlite3.connect(path)
    try:
        _create_schema(con.cursor())
        ids: dict[str, int] = {}
        for i, (fund, source, weights) in enumerate(SCHEMES, start=1):
            cur = con.execute(
                "INSERT INTO schemes (key, amc, fund_name, source, as_of) VALUES (?,?,?,?,?)",
                (f"ladder-{i}", AMC, fund, source, AS_OF),
            )
            ids[fund] = cur.lastrowid
            for w, weight in enumerate(weights, start=1):
                con.execute(
                    "INSERT INTO holdings (scheme_id, amc, fund_name, company, isin, "
                    "market_value, percent_nav, source, as_of) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ids[fund], AMC, fund, f"Company {i} No {w}",
                     f"INE0LADDER{i:02d}{w}", 1000.0 * w, weight, source, AS_OF),
                )
        con.commit()
    finally:
        con.close()
    return ids


def _holding_rows(fund: str, weights: tuple[float, ...]) -> list[dict]:
    return [
        {
            "company": f"Company {fund} {i}",
            "isin": f"INE0L{i:02d}00000{i}",
            "market_value": 1000.0 * i,
            "percent_nav": weight,
        }
        for i, weight in enumerate(weights, start=1)
    ]


def _seed_parsed_root(root: Path) -> Path:
    """Parsed stores (inline dicts, real merge shapes) feeding the document-class lookup."""
    monthly = root / "amc_websites" / "Ladder_AMC" / "2026" / "07"
    monthly.mkdir(parents=True)
    (monthly / "Monthly_Portfolio_July_2026.json").write_text(
        json.dumps({
            "amc_name": AMC,
            "schemes": {
                FUND_T0: {"fund_name": FUND_T0, "date": AS_OF,
                          "holdings": _holding_rows(FUND_T0, FULL_97)},
                FUND_CLOSED: {"fund_name": FUND_CLOSED, "date": AS_OF,
                              "holdings": _holding_rows(FUND_CLOSED, PARTIAL_60)},
                FUND_SUSPECT: {"fund_name": FUND_SUSPECT, "date": AS_OF,
                               "holdings": _holding_rows(FUND_SUSPECT, SUSPECT_130)},
            },
        }),
        encoding="utf-8",
    )
    ak = root / "advisorkhoj"
    ak.mkdir(parents=True)
    (ak / "ak_factsheet_top10.json").write_text(
        json.dumps({
            "amc": AMC,
            "files": [{
                "sheets": [{
                    "scheme": FUND_T1,
                    "date": AS_OF,
                    "plans": {"Direct": {"holdings": _holding_rows(FUND_T1, TOP10_45)}},
                }],
            }],
        }),
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# Real-path guard (E): existence+stat snapshot of everything the audit
# defaults could touch outside the sandbox
# ---------------------------------------------------------------------------

def _stat_of(path: Path) -> tuple:
    if not path.exists():
        return ("absent",)
    st = path.stat()
    return ("present", st.st_size, st.st_mtime_ns)


def _snapshot_real_data() -> dict:
    reference = Path("data/reference")
    paths = [
        Path("data/webapp.db"),
        Path("data/logs/escalation_queue.jsonl"),
        reference / "integrity_tiers.json",
        reference / "manual_intervention.csv",
        *sorted(reference.glob("*.csv")),
    ]
    return {str(p): _stat_of(p) for p in paths}


# ---------------------------------------------------------------------------
# Injected channel fakes (zero network, zero filesystem)
# ---------------------------------------------------------------------------

class RecordingDownloader:
    """``amc_recheck`` downloader fake: records every probe, never finds a document."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, attempt, ticket, session):
        self.calls.append((ticket.queue_id, attempt.location.key))
        return []


class ScriptedProvider:
    """``web_search`` provider fake (OQ-10): records queries, returns canned URLs."""

    def __init__(self, urls: list[str]) -> None:
        self.calls: list[str] = []
        self.urls = list(urls)

    def search(self, query: str, *, limit: int = 10):
        self.calls.append(query)
        return list(self.urls)


class RecordingFetcher:
    """``web_search`` fetcher fake: records EVERY url (the AC-19 proof)."""

    def __init__(self, path: str) -> None:
        self.calls: list[str] = []
        self.path = path

    def __call__(self, url, ticket, session):
        self.calls.append(url)
        return [self.path]


class ScriptedParser:
    """``parse`` fake: records (path, strategy), returns the canned payload."""

    def __init__(self, payloads: dict[str, dict]) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payloads = payloads

    def __call__(self, path, strategy, session):
        self.calls.append((path, strategy))
        return self.payloads[path]


def _t0_payload(scheme: str) -> dict:
    """Complete full-disclosure parse for ``scheme`` (Σ=97 -> tier T0)."""
    return {
        "schemes": {
            scheme: {
                "fund_name": scheme,
                "date": AS_OF,
                "holdings": _holding_rows(scheme, FULL_97),
            }
        }
    }


def _work_pending(queue: EscalationQueue, channel_runner, amc: str):
    """Mirror ``Agent._work_escalations``: dispatch every OPEN ticket of ``amc``
    to ``next_channel(ticket)`` and record the attempt (the queue is the only
    mutation, exactly like the agent)."""
    worked = []
    for ticket in [t for t in queue.load().values()
                   if t.amc == amc and t.status == STATUS_OPEN]:
        channel = next_channel(ticket)
        result = channel_runner(channel, ticket, failed_strategy=None)
        queue.record_attempt(ticket, channel, result.success)
        worked.append((ticket.queue_id, channel, result))
    return worked


# ---------------------------------------------------------------------------
# The end-to-end ladder
# ---------------------------------------------------------------------------

def test_escalation_ladder_end_to_end(tmp_path):
    db_path = tmp_path / "webapp.db"
    parsed_root = _seed_parsed_root(tmp_path / "parsed")
    report_dir = tmp_path / "reports"
    tiers_out = tmp_path / "reference" / "integrity_tiers.json"
    queue_path = tmp_path / "logs" / "escalation_queue.jsonl"
    manual_csv = tmp_path / "reference" / "manual_intervention.csv"
    trusted_path = str(tmp_path / "fetched" / "monthly_portfolio_July_2026.xlsx")

    real_before = _snapshot_real_data()
    ids = _make_db(db_path)

    # -- A: audit -> tiers + reports + sidecar + EXACTLY the two T2 tickets --
    report = run_audit(
        db_path,
        parsed_root,
        report_dir=report_dir,
        tiers_out=tiers_out,
        queue_path=queue_path,
        manual_csv_path=manual_csv,
        audit_date=AUDIT_DATE,
    )
    assert report["n_schemes"] == 4
    assert report["tier_histogram"] == {"T0": 1, "T1": 1, "T2": 2, "T3": 0}
    assert report["document_classes"] == {"factsheet_topn": 1, "full_portfolio": 3}
    assert report["flags"] == {"SUSPECT_SCALE": 1}
    assert report["escalate_true_schemes"] == 2
    assert report["escalations_enqueued"] == 2
    assert report["escalations_blocked_non_t2"] == 0
    assert report["escalation_queue_path"] == str(queue_path)
    assert (report_dir / "integrity_2026-10-05.json").exists()
    assert (report_dir / "integrity_2026-10-05.md").exists()

    sidecar = json.loads(tiers_out.read_text(encoding="utf-8"))
    assert set(sidecar) == {
        f"{AMC}|{FUND_T0}|{MONTH}",
        f"{AMC}|{FUND_T1}|{MONTH}",
        f"{AMC}|{FUND_CLOSED}|{MONTH}",
        f"{AMC}|{FUND_SUSPECT}|{MONTH}",
    }
    assert sidecar[f"{AMC}|{FUND_T0}|{MONTH}"]["tier_code"] == "T0"
    assert sidecar[f"{AMC}|{FUND_T0}|{MONTH}"]["document_class"] == "full_portfolio"
    assert sidecar[f"{AMC}|{FUND_T0}|{MONTH}"]["escalate"] is False
    assert sidecar[f"{AMC}|{FUND_T1}|{MONTH}"]["tier_code"] == "T1"
    assert sidecar[f"{AMC}|{FUND_T1}|{MONTH}"]["document_class"] == "factsheet_topn"
    assert sidecar[f"{AMC}|{FUND_T1}|{MONTH}"]["escalate"] is False
    assert sidecar[f"{AMC}|{FUND_CLOSED}|{MONTH}"]["tier_code"] == "T2"
    assert sidecar[f"{AMC}|{FUND_CLOSED}|{MONTH}"]["escalate"] is True
    assert sidecar[f"{AMC}|{FUND_SUSPECT}|{MONTH}"]["tier_code"] == "T2"
    assert sidecar[f"{AMC}|{FUND_SUSPECT}|{MONTH}"]["escalate"] is True
    assert "SUSPECT_SCALE" in sidecar[f"{AMC}|{FUND_SUSPECT}|{MONTH}"]["flags"]

    queue = EscalationQueue(queue_path=queue_path, manual_csv_path=manual_csv)
    folded = queue.load()
    assert set(folded) == {(AMC, FUND_CLOSED, MONTH), (AMC, FUND_SUSPECT, MONTH)}
    assert (AMC, FUND_T0, MONTH) not in folded
    assert (AMC, FUND_T1, MONTH) not in folded
    t_close = folded[(AMC, FUND_CLOSED, MONTH)]
    t_park = folded[(AMC, FUND_SUSPECT, MONTH)]
    assert t_close.tier == "T2"
    assert t_close.coverage_pct == pytest.approx(60.0)
    assert t_close.document_class == "full_portfolio"
    assert t_close.status == STATUS_OPEN
    assert t_close.attempts == 0
    assert t_close.channels_tried == []
    assert t_close.first_seen != ""
    assert t_park.tier == "T2"
    assert t_park.coverage_pct == pytest.approx(130.0)
    assert t_park.status == STATUS_OPEN

    # -- B: channel ORDER through the dispatcher with injected fakes --
    downloader = RecordingDownloader()
    provider = ScriptedProvider([UNTRUSTED_URL])
    fetcher = RecordingFetcher(trusted_path)
    parser = ScriptedParser({trusted_path: _t0_payload(FUND_CLOSED)})
    dispatcher = build_dispatcher(
        downloader=downloader,
        parse=parser,
        provider=provider,
        fetcher=fetcher,
    )
    dispatch_log: list[tuple[str, str]] = []

    def logging_runner(channel, ticket, *, failed_strategy=None):
        dispatch_log.append((ticket.queue_id, channel))
        return dispatcher(channel, ticket, failed_strategy=failed_strategy)

    # Attempt 1 for BOTH tickets: amc_recheck - the fake downloader finds no
    # candidate document at any alternate location, so the channel fails.
    worked = _work_pending(queue, logging_runner, AMC)
    assert [(qid, channel) for qid, channel, _ in worked] == [
        (t_close.queue_id, "amc_recheck"),
        (t_park.queue_id, "amc_recheck"),
    ]
    for _, _, result in worked:
        assert result.channel == "amc_recheck"
        assert result.success is False
        assert result.failure_code == CODE_MISSING
        assert is_valid_code(result.failure_code)
        assert "no candidate documents" in result.reason
    assert downloader.calls and all(
        qid in {t_close.queue_id, t_park.queue_id} for qid, _ in downloader.calls
    )
    assert parser.calls == []

    # Attempt 2 for BOTH tickets: web_search - the provider resolves ONLY a
    # non-allow-listed URL, which is skipped (AC-19) and NEVER fetched.
    provider_calls_before = len(provider.calls)
    worked = _work_pending(queue, logging_runner, AMC)
    assert [(qid, channel) for qid, channel, _ in worked] == [
        (t_close.queue_id, "web_search"),
        (t_park.queue_id, "web_search"),
    ]
    for _, _, result in worked:
        assert result.channel == "web_search"
        assert result.success is False
        assert result.failure_code == CODE_MISSING
        assert is_valid_code(result.failure_code)
        assert "skipped_untrusted_host" in result.reason
        assert "evil.example" in result.reason
    assert provider.calls[provider_calls_before:provider_calls_before + 3] == [
        candidate.query for candidate in plan_queries(t_close)
    ]
    assert fetcher.calls == []
    assert parser.calls == []

    # The closing probe: the ladder re-probes web_search and the provider now
    # resolves an ALLOW-LISTED AMFI URL whose parse is a complete T0
    # full_portfolio - the channel succeeds and the attempt closes the ticket.
    provider.urls = [TRUSTED_URL]
    t_close_now = queue.load()[(AMC, FUND_CLOSED, MONTH)]
    result = logging_runner("web_search", t_close_now, failed_strategy=None)
    assert result.channel == "web_search"
    assert result.success is True
    assert result.failure_code is None
    assert "full_portfolio" in result.reason
    assert "97.00%" in result.reason
    assert "amfiindia.com" in result.reason
    assert fetcher.calls == [TRUSTED_URL]
    assert parser.calls == [(trusted_path, result.strategy_used)]
    queue.record_attempt(t_close_now, "web_search", result.success)

    folded = queue.load()
    t_close_folded = folded[(AMC, FUND_CLOSED, MONTH)]
    assert t_close_folded.queue_id == t_close.queue_id
    assert t_close_folded.status == STATUS_CLOSED
    assert t_close_folded.attempts == 3
    assert t_close_folded.channels_tried == ["amc_recheck", "web_search"]

    # -- C: the other ticket walks every remaining fetch channel and parks
    #      MANUAL at the N_max cap through failing channels --
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    for channel in fetch_channels[2:N_MAX]:
        worked = _work_pending(queue, logging_runner, AMC)
        assert [(qid, walked) for qid, walked, _ in worked] == [
            (t_park.queue_id, channel)
        ]
        result = worked[0][2]
        assert result.channel == channel
        assert result.success is False
        assert result.reason.startswith("skipped_no_client")
        assert result.failure_code == CODE_MISSING_IN_DB
        assert is_valid_code(result.failure_code)

    folded = queue.load()
    t_park_folded = folded[(AMC, FUND_SUSPECT, MONTH)]
    assert t_park_folded.queue_id == t_park.queue_id
    assert t_park_folded.status == STATUS_MANUAL
    assert t_park_folded.attempts == N_MAX
    assert t_park_folded.channels_tried == fetch_channels

    close_seq = [channel for qid, channel in dispatch_log if qid == t_close.queue_id]
    park_seq = [channel for qid, channel in dispatch_log if qid == t_park.queue_id]
    assert close_seq == ["amc_recheck", "web_search", "web_search"]
    assert park_seq == fetch_channels

    row = for_queue_id(t_park.queue_id, path=manual_csv)
    assert row is not None
    assert row.status == "MANUAL"
    assert row.resolved is False
    assert row.reason_code == "ALL_CHANNELS_EXHAUSTED"
    assert row.tier == "T3"
    assert row.attempts == N_MAX
    assert row.channels_tried == tuple(fetch_channels)
    assert row.amc == AMC and row.scheme == FUND_SUSPECT and row.month == MONTH
    assert [r.queue_id for r in pending_rows(path=manual_csv)] == [t_park.queue_id]
    assert for_queue_id(t_close.queue_id, path=manual_csv) is None

    # -- D: re-audit after the fill - the closed scheme is T0, NO duplicate tickets --
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM holdings WHERE scheme_id = ?", (ids[FUND_CLOSED],))
        for weight in FULL_97:
            con.execute(
                "INSERT INTO holdings (scheme_id, amc, fund_name, company, isin, "
                "market_value, percent_nav, source, as_of) VALUES (?,?,?,?,?,?,?,?,?)",
                (ids[FUND_CLOSED], AMC, FUND_CLOSED, "Filled Industries Ltd",
                 "INE0FILLED1", 1000.0, weight, "amc_website", AS_OF),
            )
        con.commit()
    finally:
        con.close()

    queue_size_before = queue_path.stat().st_size
    report2 = run_audit(
        db_path,
        parsed_root,
        report_dir=report_dir,
        tiers_out=tiers_out,
        queue_path=queue_path,
        manual_csv_path=manual_csv,
        audit_date=REAUDIT_DATE,
    )
    assert report2["tier_histogram"] == {"T0": 2, "T1": 1, "T2": 1, "T3": 0}
    assert report2["flags"] == {"SUSPECT_SCALE": 1}
    assert report2["escalate_true_schemes"] == 1
    assert report2["escalations_enqueued"] == 0
    assert report2["escalations_blocked_non_t2"] == 0
    assert (report_dir / "integrity_2026-10-06.json").exists()
    assert (report_dir / "integrity_2026-10-06.md").exists()

    sidecar2 = json.loads(tiers_out.read_text(encoding="utf-8"))
    assert sidecar2[f"{AMC}|{FUND_CLOSED}|{MONTH}"]["tier_code"] == "T0"
    assert sidecar2[f"{AMC}|{FUND_CLOSED}|{MONTH}"]["escalate"] is False
    assert sidecar2[f"{AMC}|{FUND_CLOSED}|{MONTH}"]["coverage_pct"] == pytest.approx(97.0)

    folded = queue.load()
    assert set(folded) == {(AMC, FUND_CLOSED, MONTH), (AMC, FUND_SUSPECT, MONTH)}
    assert folded[(AMC, FUND_CLOSED, MONTH)].queue_id == t_close.queue_id
    assert folded[(AMC, FUND_CLOSED, MONTH)].status == STATUS_CLOSED
    assert folded[(AMC, FUND_SUSPECT, MONTH)].queue_id == t_park.queue_id
    assert folded[(AMC, FUND_SUSPECT, MONTH)].status == STATUS_MANUAL
    assert queue_path.stat().st_size == queue_size_before
    assert [r.queue_id for r in pending_rows(path=manual_csv)] == [t_park.queue_id]

    # -- E: the real data/ tree was neither created nor modified --
    assert _snapshot_real_data() == real_before
