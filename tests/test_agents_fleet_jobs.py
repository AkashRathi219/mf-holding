"""Scheduler dispatch for the agent fleet (src/agents/fleet_jobs.py).

Contract under test:

* ``agent_fleet_fn`` dispatches all four job ids through ``JOB_HANDLERS``
  (every handler individually monkeypatchable) and never raises on an
  unknown job name - it returns ``ok=False`` + ``error="unknown_job"``.
* ``ticket_priority`` / ``select_tickets`` order the ladder work oldest
  month first, then lowest coverage, OPEN tickets only, with an optional
  bounded prefix.
* The digest writer lands in an overridable reports dir and writes an
  explicit empty digest when the register has no pending rows.
* ``weekly_mf_holdings`` runs fleet BEFORE audit BEFORE ladder (strict
  policy order, asserted with monkeypatched stage handlers) and never calls
  the AMFI/Advisorkhoj source channels directly - the ladder stage goes
  through ``next_channel()`` so ordering is enforced by the queue.
* The corporate-actions handler delegates to the stock-refresh actions path.
* The queue-touching handlers (mf-holdings ladder / drain) drive the REAL
  ``EscalationQueue`` file protocol on tmp_path with a fake
  ``channel_runner`` - zero network, zero real channels.
* The fleet and integrity handlers forward their kwargs to the owning
  module (monkeypatched) and fold its summary into the job detail.

No network anywhere: heavy collaborators are monkeypatched or replaced by
fakes honouring the dispatcher contract ``runner(channel, ticket, *,
failed_strategy=None) -> result with .success``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.agents import fleet_jobs, integrity, runner
from src.agents.escalation import (
    CHANNELS,
    N_MAX,
    REASON_ALL_CHANNELS_EXHAUSTED,
    REASON_NOT_PUBLISHED_BY_AMC,
    STATUS_CLOSED,
    STATUS_MANUAL,
    STATUS_OPEN,
    EscalationQueue,
    Ticket,
    next_channel,
)
from src.agents.episodes import DEFAULT_ROOT as EPISODE_ROOT

KNOWN_JOBS = (
    "daily_integrity_audit",
    "daily_corporate_actions",
    "weekly_mf_holdings",
    "weekly_manual_digest",
)


def _queue(tmp_path: Path) -> EscalationQueue:
    return EscalationQueue(
        queue_path=tmp_path / "escalation_queue.jsonl",
        manual_csv_path=tmp_path / "manual_intervention.csv",
    )


def _enqueue(queue: EscalationQueue, amc: str, scheme: str, month: str,
             coverage: float = 60.0) -> Ticket:
    ticket = queue.enqueue(amc, scheme, month, "T2", coverage, "full_portfolio")
    assert ticket is not None
    return ticket


def _ticket(month: str, coverage: float, amc: str = "AMC A",
            scheme: str = "Fund A", queue_id: str = "ESC-x") -> Ticket:
    return Ticket(
        queue_id=queue_id, amc=amc, scheme=scheme, month=month,
        tier="T2", coverage_pct=coverage, document_class="full_portfolio",
    )


class _FakeResult:
    """Dispatcher-contract stand-in: only ``success`` is read by the sweep."""

    def __init__(self, success: bool):
        self.success = success


# ---------------------------------------------------------------------------
# 1. agent_fleet_fn dispatches every known job through JOB_HANDLERS
# ---------------------------------------------------------------------------

def test_agent_fleet_fn_ok_on_all_known_jobs(monkeypatch):
    calls: list[dict] = []

    def fake_handler(**kwargs):
        calls.append(kwargs)
        return {"detail_marker": "fake"}

    for name in KNOWN_JOBS:
        monkeypatch.setitem(fleet_jobs.JOB_HANDLERS, name, fake_handler)

    for name in KNOWN_JOBS:
        result = fleet_jobs.agent_fleet_fn(name, production=True)
        assert result == {"job": name, "ok": True, "detail": {"detail_marker": "fake"}}

    assert len(calls) == 4
    assert calls[0] == {"production": True}  # kwargs forwarded untouched


# ---------------------------------------------------------------------------
# 2. unknown job name: ok=False + error, never raises
# ---------------------------------------------------------------------------

def test_agent_fleet_fn_unknown_job_returns_error_without_raising():
    result = fleet_jobs.agent_fleet_fn("no_such_job", production=True)
    assert result == {"job": "no_such_job", "ok": False, "error": "unknown_job"}


# ---------------------------------------------------------------------------
# 3./4. select_tickets: OPEN only, priority-ordered, optionally truncated
# ---------------------------------------------------------------------------

def test_select_tickets_returns_only_open_tickets(tmp_path):
    queue = _queue(tmp_path)
    kept = _enqueue(queue, "AMC A", "Fund A", "2026-08")
    closed = _enqueue(queue, "AMC B", "Fund B", "2026-08")
    manual = _enqueue(queue, "AMC C", "Fund C", "2026-08")
    queue.record_attempt(closed, "amc_recheck", True)   # success -> CLOSED
    queue.park(manual, REASON_NOT_PUBLISHED_BY_AMC)     # parked -> MANUAL

    selected = fleet_jobs.select_tickets(queue)

    assert [t.queue_id for t in selected] == [kept.queue_id]
    assert all(t.status == STATUS_OPEN for t in selected)


def test_select_tickets_truncates_to_highest_priority(tmp_path):
    queue = _queue(tmp_path)
    _enqueue(queue, "AMC New", "Fund New", "2026-09", 10.0)   # newest month
    keep_old = _enqueue(queue, "AMC Old", "Fund Old", "2026-06", 90.0)
    keep_low = _enqueue(queue, "AMC Mid", "Fund Mid", "2026-08", 5.0)

    selected = fleet_jobs.select_tickets(queue, max_tickets=2)

    # month dominates coverage: the old 90%-coverage ticket outranks the
    # newer low-coverage one; the cap keeps the priority prefix.
    assert [t.queue_id for t in selected] == [keep_old.queue_id, keep_low.queue_id]
    assert fleet_jobs.select_tickets(queue, max_tickets=0) == []


# ---------------------------------------------------------------------------
# 5. ticket_priority: older month first, then lower coverage; deterministic
# ---------------------------------------------------------------------------

def test_ticket_priority_orders_older_month_first_then_lower_coverage():
    older = _ticket("2026-07", 90.0, queue_id="ESC-old")
    newer = _ticket("2026-08", 10.0, queue_id="ESC-new")
    assert fleet_jobs.ticket_priority(older) < fleet_jobs.ticket_priority(newer)

    low = _ticket("2026-08", 40.0, queue_id="ESC-low")
    high = _ticket("2026-08", 60.0, queue_id="ESC-high")
    assert fleet_jobs.ticket_priority(low) < fleet_jobs.ticket_priority(high)

    # deterministic tie-break: amc, then scheme
    tie_a = _ticket("2026-08", 50.0, amc="AMC A", scheme="Fund B", queue_id="ESC-ta")
    tie_b = _ticket("2026-08", 50.0, amc="AMC B", scheme="Fund A", queue_id="ESC-tb")
    assert fleet_jobs.ticket_priority(tie_a) < fleet_jobs.ticket_priority(tie_b)

    # an unparseable month is the oldest possible month (worked first)
    unknown = _ticket("unknown", 10.0, queue_id="ESC-unk")
    assert fleet_jobs.ticket_priority(unknown) < fleet_jobs.ticket_priority(older)


# ---------------------------------------------------------------------------
# 6. manual digest: real rows + explicit empty digest
# ---------------------------------------------------------------------------

def test_manual_digest_writes_pending_rows_to_tmp_reports_dir(tmp_path):
    queue = _queue(tmp_path)
    not_published = _enqueue(queue, "Test AMC", "Test Fund", "2026-08", 55.0)
    queue.park(not_published, REASON_NOT_PUBLISHED_BY_AMC)
    exhausted = _enqueue(queue, "Other AMC", "Other Fund", "2026-07", 70.0)
    fetch_channels = [c for c in CHANNELS if c != "manual"]
    for channel in fetch_channels[:N_MAX]:
        queue.record_attempt(exhausted, channel, False)
    assert exhausted.status == STATUS_MANUAL  # parked at the N_MAX cap

    reports = tmp_path / "reports"
    detail = fleet_jobs.write_manual_digest(
        report_dir=reports,
        manual_csv_path=queue.manual_csv_path,
        digest_date=date(2026, 10, 5),
    )

    path = reports / "manual_digest_2026-10-05.md"
    assert detail["pending"] == 2
    assert detail["counts"] == {
        REASON_NOT_PUBLISHED_BY_AMC: 1,
        REASON_ALL_CHANNELS_EXHAUSTED: 1,
    }
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Test Fund" in text and "Other Fund" in text
    assert "NOT_PUBLISHED_BY_AMC" in text
    assert "ALL_CHANNELS_EXHAUSTED" in text


def test_manual_digest_writes_empty_digest_when_no_pending_rows(tmp_path):
    reports = tmp_path / "reports"
    missing_csv = tmp_path / "missing" / "manual_intervention.csv"

    detail = fleet_jobs.write_manual_digest(
        report_dir=reports, manual_csv_path=missing_csv, digest_date=date(2026, 10, 5))

    path = reports / "manual_digest_2026-10-05.md"
    assert detail["pending"] == 0
    assert detail["counts"] == {}
    assert path.exists()  # an empty digest is written, never nothing
    assert "Pending rows: 0" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 7. the handler registry is complete and dispatches the real handlers
# ---------------------------------------------------------------------------

def test_all_handlers_reachable_through_job_handlers():
    assert tuple(sorted(fleet_jobs.JOB_HANDLERS)) == tuple(sorted(KNOWN_JOBS))
    assert fleet_jobs.JOB_HANDLERS == {
        "daily_integrity_audit": fleet_jobs.run_integrity_audit,
        "daily_corporate_actions": fleet_jobs.run_corporate_actions,
        "weekly_mf_holdings": fleet_jobs.run_mf_holdings,
        "weekly_manual_digest": fleet_jobs.write_manual_digest,
    }
    for name, handler in fleet_jobs.JOB_HANDLERS.items():
        assert callable(handler), name


# ---------------------------------------------------------------------------
# 8. weekly_mf_holdings: fleet BEFORE audit BEFORE ladder, and the source
#    channels are NEVER a direct/parallel activity
# ---------------------------------------------------------------------------

def test_mf_holdings_runs_fleet_before_audit_before_ladder(monkeypatch):
    calls: list[str] = []
    captured: dict[str, dict] = {}

    def stage(name, detail):
        def _stage(**kwargs):
            calls.append(name)
            captured[name] = kwargs
            return detail
        return _stage

    monkeypatch.setattr(fleet_jobs, "run_agent_fleet", stage("fleet", {"total": 3}))
    monkeypatch.setattr(
        fleet_jobs, "run_integrity_audit", stage("audit", {"schemes_audited": 12}))
    monkeypatch.setattr(fleet_jobs, "run_ladder_drain", stage("ladder", {"worked": 2}))

    detail = fleet_jobs.run_mf_holdings(production=True)

    assert calls == ["fleet", "audit", "ladder"]  # strict policy order
    assert captured["fleet"]["production"] is True  # real production bindings
    assert detail == {
        "fleet": {"total": 3},
        "audit": {"schemes_audited": 12},
        "ladder": {"worked": 2},
    }


def test_mf_holdings_records_stage_errors_without_blocking_later_stages(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("fleet exploded")

    calls: list[str] = []

    monkeypatch.setattr(fleet_jobs, "run_agent_fleet", boom)
    monkeypatch.setattr(
        fleet_jobs,
        "run_integrity_audit",
        lambda **kw: calls.append("audit") or {"schemes_audited": 0},
    )
    monkeypatch.setattr(
        fleet_jobs,
        "run_ladder_drain",
        lambda **kw: calls.append("ladder") or {"worked": 0},
    )

    detail = fleet_jobs.run_mf_holdings(production=True)

    assert detail["fleet"] == {"error": "fleet exploded"}
    assert calls == ["audit", "ladder"]  # a fleet failure never blocks the rest


def test_mf_holdings_never_calls_source_channels_directly(monkeypatch, tmp_path):
    from src.agents import source_advisorkhoj, source_amfi

    queue = _queue(tmp_path)
    fresh = _enqueue(queue, "AMC Fresh", "Fund Fresh", "2026-08")  # next: amc_recheck
    at_amfi = _enqueue(queue, "AMC Amfi", "Fund Amfi", "2026-07")
    queue.record_attempt(at_amfi, "amc_recheck", False)
    queue.record_attempt(at_amfi, "web_search", False)
    assert next_channel(at_amfi) == "amfi"

    direct: list[str] = []
    monkeypatch.setattr(source_amfi, "run", lambda *a, **k: direct.append("amfi"))
    monkeypatch.setattr(
        source_advisorkhoj, "run", lambda *a, **k: direct.append("advisorkhoj"))

    # fleet + audit are patched out (zero network); the LADDER stage runs the
    # real next_channel()/queue protocol with a fake runner.
    monkeypatch.setattr(fleet_jobs, "run_agent_fleet", lambda **kw: {"total": 0})
    monkeypatch.setattr(
        fleet_jobs, "run_integrity_audit", lambda **kw: {"schemes_audited": 0})

    worked: list[tuple[str, str]] = []

    def fake_runner(channel, ticket, *, failed_strategy=None):
        worked.append((ticket.amc, channel))
        return _FakeResult(success=False)

    detail = fleet_jobs.run_mf_holdings(
        queue_path=queue.queue_path,
        manual_csv_path=queue.manual_csv_path,
        channel_runner=fake_runner,
    )

    assert direct == []  # AMFI/Advisorkhoj never a parallel/peer activity
    assert detail["fleet"] == {"total": 0}
    assert detail["audit"] == {"schemes_audited": 0}
    assert detail["ladder"]["worked"] == 2
    # each ticket was worked at exactly its own next_channel() rung through
    # the runner - the queue enforces amc_recheck BEFORE amfi
    assert ("AMC Fresh", "amc_recheck") in worked
    assert ("AMC Amfi", "amfi") in worked
    assert queue.load()[(fresh.amc, fresh.scheme, fresh.month)].attempts == 1


# ---------------------------------------------------------------------------
# 9. daily_corporate_actions delegates to the stock-refresh actions path
# ---------------------------------------------------------------------------

def test_corporate_actions_handler_delegates_to_stock_actions_path(monkeypatch):
    from src import stock_actions

    captured: dict = {}

    def fake_run(ident=None, symbols=None, limit=None):
        captured["symbols"] = symbols
        captured["limit"] = limit
        return [{"status": "ok"}, {"status": "kept_previous"}, {"status": "no_symbol"}]

    monkeypatch.setattr(stock_actions, "run", fake_run)

    detail = fleet_jobs.run_corporate_actions(limit=2)

    assert captured["symbols"] is None
    assert captured["limit"] == 2
    assert detail == {"stocks_worked": 3, "actions_ok": 1}


# ---------------------------------------------------------------------------
# handler wiring: queue work with a fake channel_runner (zero network)
# ---------------------------------------------------------------------------

def test_ladder_drain_works_all_open_tickets_and_closes_on_success(tmp_path):
    queue = _queue(tmp_path)
    a = _enqueue(queue, "AMC A", "Fund A", "2026-06")
    b = _enqueue(queue, "AMC B", "Fund B", "2026-07")
    done = _enqueue(queue, "AMC C", "Fund C", "2026-08")
    queue.record_attempt(done, "amc_recheck", True)  # already CLOSED: not re-worked

    detail = fleet_jobs.run_ladder_drain(
        queue_path=queue.queue_path,
        manual_csv_path=queue.manual_csv_path,
        channel_runner=lambda channel, ticket, *, failed_strategy=None:
            _FakeResult(success=True),
    )

    assert detail["worked"] == 2
    assert detail["closed"] == 2
    folded = queue.load()
    assert folded[(a.amc, a.scheme, a.month)].status == STATUS_CLOSED
    assert folded[(b.amc, b.scheme, b.month)].status == STATUS_CLOSED


# ---------------------------------------------------------------------------
# handler wiring: fleet + integrity forward to their owning modules
# ---------------------------------------------------------------------------

def test_agent_fleet_handler_forwards_production_and_folds_summary(monkeypatch):
    captured: dict = {}

    class _Summary:
        total, succeeded, failed, dry_run = 3, 2, 1, False
        errors = [{"amc": "AMC X", "error": "RuntimeError: boom"}]

    def fake_run_all(amc_names=None, **kwargs):
        captured["amc_names"] = amc_names
        captured.update(kwargs)
        return _Summary()

    monkeypatch.setattr(runner, "run_all", fake_run_all)

    detail = fleet_jobs.run_agent_fleet(production=True, month="2026-09")

    assert captured["production"] is True
    assert captured["month"] == "2026-09"
    assert captured["episode_root"] == EPISODE_ROOT  # scheduled runs journal too
    assert detail["total"] == 3
    assert detail["succeeded"] == 2
    assert detail["failed"] == 1
    assert detail["errors"] == [{"amc": "AMC X", "error": "RuntimeError: boom"}]


def test_integrity_audit_handler_forwards_paths_and_folds_report(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_audit(**kwargs):
        captured.update(kwargs)
        return {
            "n_schemes": 12,
            "escalations_enqueued": 3,
            "tiers_entries": 900,
            "audit_date": "2026-10-05",
        }

    monkeypatch.setattr(integrity, "run_audit", fake_run_audit)

    detail = fleet_jobs.run_integrity_audit(
        db_path=tmp_path / "webapp.db", report_dir=tmp_path / "reports")

    assert captured["db_path"] == tmp_path / "webapp.db"
    assert captured["report_dir"] == tmp_path / "reports"
    assert "parsed_root" not in captured  # unset paths fall back to auditor defaults
    assert detail["schemes_audited"] == 12
    assert detail["escalations_enqueued"] == 3
