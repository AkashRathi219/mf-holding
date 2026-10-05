"""Scheduler wiring for the AMC agent fleet (SPEC §8.1/§11 + the daily/weekly
data policy): four jobs, one injectable dispatch entry point.

Contract under test (src/scheduler.py):

* ``setup()`` registers the agent-fleet jobs iff ``agent_fleet_fn`` is
  injected AND ``scheduler.agent_fleet.enabled`` is true (default true).
* Daily jobs carry an hour/minute cron trigger and NO day_of_week; weekly
  jobs carry the configured day_of_week.
* Each job forwards its own kwargs (the weekly production flag) through to
  ``agent_fleet_fn``.
* A raising ``agent_fleet_fn`` is contained: logged, never propagated, the
  scheduler keeps running.

No network, no real scheduler start: ``setup()`` only registers pending
jobs on an un-started AsyncIOScheduler.
"""

from __future__ import annotations

import asyncio
import logging

from src.scheduler import MonthlyScheduler

AGENT_JOB_IDS = {
    "daily_integrity_audit",
    "daily_corporate_actions",
    "weekly_mf_holdings",
    "weekly_manual_digest",
}

# Retired ids: the daily ladder sweep / weekly source channels were folded
# into weekly_mf_holdings, and the standalone weekly fleet + drain jobs are
# its stages now. None of them may register again.
REMOVED_JOB_IDS = {
    "daily_ladder_sweep",
    "weekly_source_channels",
    "weekly_agent_fleet",
    "weekly_ladder_drain",
}


def _make(agent_fleet_fn, agent_fleet=None, tmp_path=None) -> MonthlyScheduler:
    sched_cfg: dict = {"enabled": True}
    if agent_fleet is not None:
        sched_cfg["agent_fleet"] = agent_fleet
    sched = MonthlyScheduler(
        lambda **kwargs: None,
        {"scheduler": sched_cfg},
        base_dir=tmp_path,
        agent_fleet_fn=agent_fleet_fn,
    )
    sched.setup()
    return sched


def _job_ids(sched: MonthlyScheduler) -> set[str]:
    return {job.id for job in sched.scheduler.get_jobs()}


def _fields(trigger) -> dict[str, str]:
    return {field.name: str(field) for field in trigger.fields}


def test_setup_registers_all_agent_jobs(tmp_path):
    sched = _make(lambda name, **kw: None, {"enabled": True}, tmp_path)
    ids = _job_ids(sched)
    assert AGENT_JOB_IDS <= ids
    assert ids - AGENT_JOB_IDS == {"monthly_holdings_fetch"}
    assert not ids & REMOVED_JOB_IDS  # removed ids are NOT registered


def test_no_agent_jobs_without_agent_fleet_fn(tmp_path):
    sched = _make(None, {"enabled": True}, tmp_path)
    assert not _job_ids(sched) & AGENT_JOB_IDS
    assert "monthly_holdings_fetch" in _job_ids(sched)  # non-agent job intact


def test_no_agent_jobs_when_block_disabled(tmp_path):
    sched = _make(lambda name, **kw: None, {"enabled": False}, tmp_path)
    assert not _job_ids(sched) & AGENT_JOB_IDS


def test_daily_and_weekly_triggers_follow_config(tmp_path):
    sched = _make(
        lambda name, **kw: None,
        {
            "enabled": True,
            "daily_integrity_audit": {"hour": 5, "minute": 45},
            "weekly_mf_holdings": {"day_of_week": "thu", "hour": 9, "minute": 15},
        },
        tmp_path,
    )
    jobs = {job.id: job for job in sched.scheduler.get_jobs()}
    daily = _fields(jobs["daily_integrity_audit"].trigger)
    assert (daily["hour"], daily["minute"]) == ("5", "45")
    assert daily["day_of_week"] == "*"  # daily trigger has NO day_of_week
    weekly = _fields(jobs["weekly_mf_holdings"].trigger)
    assert weekly["day_of_week"] == "thu"  # weekly carries the configured day
    assert (weekly["hour"], weekly["minute"]) == ("9", "15")
    # untouched jobs keep their documented defaults
    assert _fields(jobs["weekly_manual_digest"].trigger)["day_of_week"] == "fri"
    # corporate actions default 21:45 - distinct from the 21:00 stock job
    corp = _fields(jobs["daily_corporate_actions"].trigger)
    assert (corp["hour"], corp["minute"], corp["day_of_week"]) == ("21", "45", "*")


def test_per_job_kwargs_pass_through(tmp_path):
    sched = _make(lambda name, **kw: None, {"enabled": True}, tmp_path)
    jobs = {job.id: job for job in sched.scheduler.get_jobs()}
    assert jobs["weekly_mf_holdings"].kwargs == {"production": True}
    assert jobs["daily_corporate_actions"].kwargs == {}
    assert jobs["daily_integrity_audit"].kwargs == {}
    assert jobs["weekly_manual_digest"].kwargs == {}


def test_run_agent_job_dispatches_name_and_kwargs(tmp_path):
    calls: list[tuple] = []

    def fleet(name, **kwargs):
        calls.append((name, kwargs))

    sched = _make(fleet, {"enabled": True}, tmp_path)
    asyncio.run(sched._run_agent_job("daily_corporate_actions"))
    asyncio.run(sched._run_agent_job("weekly_mf_holdings", production=True))
    assert calls == [
        ("daily_corporate_actions", {}),
        ("weekly_mf_holdings", {"production": True}),
    ]


def test_agent_job_exception_caught_and_logged(tmp_path, caplog):
    def boom(name, **kwargs):
        raise RuntimeError("agent fleet exploded")

    sched = _make(boom, {"enabled": True}, tmp_path)
    with caplog.at_level(logging.ERROR, logger="src.scheduler"):
        asyncio.run(sched._run_agent_job("daily_integrity_audit"))
    errors = [
        rec for rec in caplog.records
        if rec.levelno == logging.ERROR
        and "daily_integrity_audit" in rec.getMessage()
    ]
    assert errors, "the failure must be logged, not silently swallowed"

    # the scheduler survives: a follow-up good run still executes
    seen: list[str] = []
    sched.agent_fleet_fn = lambda name, **kw: seen.append(name)
    asyncio.run(sched._run_agent_job("daily_integrity_audit"))
    assert seen == ["daily_integrity_audit"]
