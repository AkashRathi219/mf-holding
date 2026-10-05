"""Scheduler dispatch for the AMC agent-fleet jobs (SPEC §8.1/§11 + the
daily/weekly data policy).

``src.scheduler.MonthlyScheduler`` registers the agent-fleet jobs and routes
every firing through ONE injected entry point - ``agent_fleet_fn(job_name,
**kwargs)`` (the per-job kwargs contract lives in
``scheduler.agent_fleet_job_kwargs``). Nothing in the scheduler knows about
agents; this module is the missing half: the dispatch table plus a
ready-to-wire ``agent_fleet_fn`` for ``main.py``::

    from src.agents.fleet_jobs import agent_fleet_fn

    scheduler = MonthlyScheduler(..., agent_fleet_fn=agent_fleet_fn)

Job table (ids, cadences and defaults are owned by
``scheduler._AGENT_FLEET_JOBS``; the keys of :data:`JOB_HANDLERS` must match
exactly, and every handler is a module-level function so tests and operators
can patch any single one independently):

* ``daily_integrity_audit`` (daily 07:20 IST) -> :func:`run_integrity_audit` -
  the read-only §11.1 auditor over the webapp DB + parsed stores.
* ``daily_corporate_actions`` (daily 21:45 IST) -> :func:`run_corporate_actions`
  - the stock-refresh actions path, decoupled from the bundled 21:00 stock
  job so an action refresh is never blocked by a price failure.
* ``weekly_mf_holdings`` (Mon 06:30) -> :func:`run_mf_holdings` - the WHOLE
  MF-holdings cycle, strictly ordered: (a) the AMC discovery/download fleet
  FIRST (``runner.run_all(production=True)``) so the AMC-own-website
  scrapers find and fetch the links, (b) the integrity audit so the freshly
  downloaded data is tiered, (c) ONLY THEN the escalation ladder, which per
  ticket walks ``amc_recheck -> web_search -> amfi -> advisorkhoj -> manual``
  via ``next_channel()`` - so AMFI/Advisorkhoj are reached ONLY as a
  downstream fallback after the AMC agents have had their turn. The former
  ``daily_ladder_sweep`` / ``weekly_source_channels`` / standalone
  fleet-and-drain jobs are folded in here: mutual-fund holdings work is
  WEEKLY, and the source channels must never be a parallel/peer activity.
* ``weekly_manual_digest`` (Fri 18:00) -> :func:`write_manual_digest` -
  ``data/reports/manual_digest_<YYYY-MM-DD>.md`` from the §11.4 manual
  register's pending rows plus reason counts.

Import discipline: the module top level pulls ONLY the stdlib, so wiring this
module (or merely importing it) stays cheap and importable everywhere. Every
heavy collaborator - ``integrity``, ``runner``, ``production``, ``dispatch``,
``manual_register``, ``escalation``, ``rate_limit``, ``episodes``,
``stock_actions`` - is imported inside the handler that needs it, and every
handler takes overridable path kwargs (queue path, reports dir, ...) so tests
run on ``tmp_path`` sandboxes with injected fakes and zero network. Building
the default channel dispatcher is side-effect-free too: the source client is
lazy (nothing imported, nothing connected until its first request) and the
limiter only reads local circuit-breaker state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.escalation import Ticket

logger = logging.getLogger(__name__)

# The job ids registered by src.scheduler._AGENT_FLEET_JOBS. JOB_HANDLERS
# below must carry exactly these keys (asserted by the tests).
JOB_INTEGRITY_AUDIT = "daily_integrity_audit"
JOB_CORPORATE_ACTIONS = "daily_corporate_actions"
JOB_MF_HOLDINGS = "weekly_mf_holdings"
JOB_MANUAL_DIGEST = "weekly_manual_digest"

JOB_NAMES: tuple[str, ...] = (
    JOB_INTEGRITY_AUDIT,
    JOB_CORPORATE_ACTIONS,
    JOB_MF_HOLDINGS,
    JOB_MANUAL_DIGEST,
)

# Fleet worker bound - mirrors runner.DEFAULT_MAX_AGENTS (SPEC §8.1).
DEFAULT_MAX_AGENTS = 5
DEFAULT_REPORTS_DIR = Path("data/reports")

_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")

__all__ = [
    "DEFAULT_MAX_AGENTS",
    "DEFAULT_REPORTS_DIR",
    "JOB_HANDLERS",
    "JOB_NAMES",
    "agent_fleet_fn",
    "run_agent_fleet",
    "run_corporate_actions",
    "run_integrity_audit",
    "run_ladder_drain",
    "run_mf_holdings",
    "select_tickets",
    "ticket_priority",
    "write_manual_digest",
]


def ticket_priority(ticket) -> tuple[int, float, str, str]:
    """Deterministic ladder sort key: oldest month first, then lowest coverage.

    ``(month_epoch, coverage_pct, amc, scheme)``. The ``YYYY-MM`` bucket maps
    to a monotonic epoch (``year * 12 + month``) so older months sort first
    without string tricks; an absent or unparseable month (the auditor's
    ``"unknown"`` bucket) maps to epoch 0 - the oldest possible month - so
    unkeyable scheme-months are worked before anything else and cannot
    silently rot at the bottom of the queue. Ties break on ``amc`` then
    ``scheme``, making the sweep order fully deterministic.
    """
    month = str(getattr(ticket, "month", "") or "").strip()
    match = _MONTH_RE.match(month)
    if match:
        year, mon = int(match.group(1)), int(match.group(2))
        month_epoch = year * 12 + mon if 1 <= mon <= 12 else 0
    else:
        month_epoch = 0
    return (
        month_epoch,
        float(getattr(ticket, "coverage_pct", 0.0) or 0.0),
        str(getattr(ticket, "amc", "") or ""),
        str(getattr(ticket, "scheme", "") or ""),
    )


def select_tickets(queue, *, max_tickets: int | None = None) -> list[Ticket]:
    """The queue's OPEN tickets in :func:`ticket_priority` order, truncated.

    CLOSED and MANUAL tickets are never returned (a parked scheme-month is
    immutable to agents, §11.4; a closed one is done). ``max_tickets`` keeps
    the highest-priority (oldest / lowest-coverage) prefix; ``None`` means
    unbounded - how the weekly ``weekly_mf_holdings`` ladder stage drains.
    """
    from src.agents.escalation import STATUS_OPEN

    tickets = [t for t in queue.load().values() if t.status == STATUS_OPEN]
    tickets.sort(key=ticket_priority)
    if max_tickets is not None:
        tickets = tickets[: max(0, int(max_tickets))]
    return tickets


def _given(**kwargs) -> dict:
    """Drop ``None`` values so each overridable path falls back to its module default."""
    return {key: value for key, value in kwargs.items() if value is not None}


def _open_queue(queue_path=None, manual_csv_path=None):
    """The escalation queue at the SPEC locations or the caller's overrides."""
    from src.agents.escalation import (
        DEFAULT_MANUAL_CSV_PATH,
        DEFAULT_QUEUE_PATH,
        EscalationQueue,
    )

    return EscalationQueue(
        queue_path=queue_path if queue_path is not None else DEFAULT_QUEUE_PATH,
        manual_csv_path=(
            manual_csv_path if manual_csv_path is not None else DEFAULT_MANUAL_CSV_PATH
        ),
    )


def _build_channel_runner(
    *,
    channel_runner=None,
    limiter=None,
    client=None,
    out_dir=None,
):
    """The §11.3 dispatcher for scheduled channel work; an injected runner wins.

    Built from ``source_channel_kwargs`` so the ``amfi`` / ``advisorkhoj``
    routes carry the lazy production client and a default shared
    ``RateLimiter`` so every dispatch honours the AC-6 circuit breaker.
    Construction is side-effect-free: no network, no heavy imports until a
    channel actually runs.
    """
    if channel_runner is not None:
        return channel_runner
    from src.agents import dispatch
    from src.agents.production import source_channel_kwargs
    from src.agents.rate_limit import RateLimiter

    return dispatch.build_dispatcher(
        limiter=limiter if limiter is not None else RateLimiter(),
        **source_channel_kwargs(client=client, out_dir=out_dir),
    )


def _work_tickets(queue, tickets, channel_runner) -> dict:
    """One §11.3 channel attempt per ticket; the queue keeps the accounting.

    Mirrors ``Agent._work_escalations`` exactly: each ticket goes to
    ``next_channel(ticket)`` with ``failed_strategy=None``, and the ONLY
    queue mutation is ``record_attempt`` - a success closes the ticket, the
    queue's own ``N_MAX`` cap parks a stalled one to MANUAL (never
    re-implemented here). Returns the small sweep summary the job detail
    carries.
    """
    from src.agents.escalation import (
        STATUS_CLOSED,
        STATUS_MANUAL,
        next_channel,
    )

    worked = closed = parked = still_open = 0
    for ticket in tickets:
        channel = next_channel(ticket)
        result = channel_runner(channel, ticket, failed_strategy=None)
        queue.record_attempt(ticket, channel, bool(result.success))
        worked += 1
        if ticket.status == STATUS_CLOSED:
            closed += 1
        elif ticket.status == STATUS_MANUAL:
            parked += 1
        else:
            still_open += 1
    return {
        "worked": worked,
        "closed": closed,
        "parked": parked,
        "still_open": still_open,
    }


def run_integrity_audit(
    *,
    db_path: str | Path | None = None,
    parsed_root: str | Path | None = None,
    report_dir: str | Path | None = None,
    tiers_out: str | Path | None = None,
    queue_path: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    enqueue: bool = True,
    limit_amc: str | None = None,
) -> dict:
    """``daily_integrity_audit``: one read-only pass of the §11.1 auditor.

    Audits WHAT THE FRONTEND DISPLAYS (webapp ``schemes``/``holdings`` opened
    through a read-only SQLite URI), writes
    ``data/reports/integrity_<date>.json``/``.md`` plus the AC-21 tier
    sidecar and enqueues new T2 escalations. No network: the parsed stores
    are read from disk. Every path is overridable; unset paths fall back to
    the auditor's own SPEC defaults.
    """
    from src.agents.integrity import run_audit

    report = run_audit(
        **_given(
            db_path=db_path,
            parsed_root=parsed_root,
            report_dir=report_dir,
            tiers_out=tiers_out,
            queue_path=queue_path,
            manual_csv_path=manual_csv_path,
        ),
        enqueue=enqueue,
        limit_amc=limit_amc,
    )
    return {
        "audit_date": report.get("audit_date"),
        "schemes_audited": int(report.get("n_schemes") or 0),
        "escalations_enqueued": int(report.get("escalations_enqueued") or 0),
        "tiers_entries": int(report.get("tiers_entries") or 0),
    }


def run_corporate_actions(
    *,
    symbols: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """``daily_corporate_actions``: stock corporate actions, decoupled from prices.

    Delegates to the stock-refresh actions path (``src.stock_actions.run`` -
    Yahoo dividend/split events plus the NSE announcement/structured-action
    feeds, kept-previous on source failure [BUG-H5]) as its own daily job so
    an action refresh is never blocked by a price failure in the bundled
    ``daily_stock_refresh`` job. The lazy import keeps wiring this module
    cheap; tests patch ``src.stock_actions.run``.
    """
    from src import stock_actions

    rows = stock_actions.run(symbols=symbols, limit=limit) or []
    ok = sum(1 for r in rows if r.get("status") == "ok")
    return {"stocks_worked": len(rows), "actions_ok": ok}


def run_agent_fleet(
    *,
    production: bool = True,
    dry_run: bool = False,
    max_agents: int = DEFAULT_MAX_AGENTS,
    amc_names: list[str] | None = None,
    month: object | None = None,
    registry_path: str | Path | None = None,
    queue_path: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    episode_root: str | Path | None = None,
    state_root: str | Path | None = None,
    register_path: str | Path | None = None,
    discover=None,
    download=None,
    parse=None,
) -> dict:
    """Fleet stage of ``weekly_mf_holdings``: one full per-AMC discovery run.

    ``runner.run_all(production=production, ...)`` - the real adapter discover,
    the real document downloader and the real AI parse, unless the
    corresponding seam is injected (which is how tests stay zero-network: an
    injected seam wins over the production bundle). The scheduler forwards
    ``production=True`` for the weekly MF-holdings cycle so the AMC-own-website
    scrapers find and fetch the statement links BEFORE the audit re-tiers and
    the escalation ladder is worked. Episodes are journalled at the runner's
    SPEC default so a scheduled run leaves the same trail the CLI does.
    Per-AMC failures never escape ``run_all``; they land in the returned
    ``errors`` fold.
    """
    from src.agents import runner
    from src.agents.episodes import DEFAULT_ROOT as DEFAULT_EPISODE_ROOT

    summary = runner.run_all(
        amc_names=amc_names,
        production=production,
        dry_run=dry_run,
        max_agents=max_agents,
        registry_path=registry_path,
        queue_path=queue_path,
        manual_csv_path=manual_csv_path,
        episode_root=(
            episode_root if episode_root is not None else DEFAULT_EPISODE_ROOT
        ),
        state_root=state_root,
        register_path=register_path,
        month=month,
        discover=discover,
        download=download,
        parse=parse,
    )
    return {
        "total": summary.total,
        "succeeded": summary.succeeded,
        "failed": summary.failed,
        "dry_run": summary.dry_run,
        "errors": list(summary.errors),
    }


def run_mf_holdings(
    *,
    production: bool = True,
    queue_path: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    channel_runner=None,
    limiter=None,
    client=None,
    out_dir=None,
) -> dict:
    """``weekly_mf_holdings``: the whole MF-holdings cycle, STRICTLY ordered.

    (a) AMC agents FIRST - :func:`run_agent_fleet` (``production=production``)
    so the AMC-own-website scrapers find and fetch the statement links;
    (b) then the integrity audit (:func:`run_integrity_audit`) so the newly
    downloaded data is tiered; (c) ONLY THEN the escalation ladder
    (:func:`run_ladder_drain`), which per ticket walks
    ``amc_recheck -> web_search -> amfi -> advisorkhoj -> manual`` via
    ``next_channel()`` - i.e. AMFI/Advisorkhoj are reached ONLY as a
    downstream fallback after the AMC agents have had their turn.

    The source channels are NEVER called directly here: the ladder stage
    goes through ``next_channel()`` and the channel runner, so the fallback
    ordering is enforced by the queue's own ``channels_tried`` accounting -
    the source channels can never be a parallel/peer activity.

    Every stage's outcome is recorded in the returned detail dict (a failing
    stage is recorded as ``{"error": ...}`` and never blocks the later
    stages - the weekly cycle must always complete its remaining stages).
    Stage handlers are module-level functions, so tests can patch any single
    one independently to assert the order.
    """
    detail: dict = {}
    try:
        detail["fleet"] = run_agent_fleet(
            production=production,
            queue_path=queue_path,
            manual_csv_path=manual_csv_path,
        )
    except Exception as e:
        detail["fleet"] = {"error": str(e)}
    try:
        detail["audit"] = run_integrity_audit(
            queue_path=queue_path,
            manual_csv_path=manual_csv_path,
        )
    except Exception as e:
        detail["audit"] = {"error": str(e)}
    try:
        detail["ladder"] = run_ladder_drain(
            queue_path=queue_path,
            manual_csv_path=manual_csv_path,
            channel_runner=channel_runner,
            limiter=limiter,
            client=client,
            out_dir=out_dir,
        )
    except Exception as e:
        detail["ladder"] = {"error": str(e)}
    return detail


def run_ladder_drain(
    *,
    queue_path: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    channel_runner=None,
    limiter=None,
    client=None,
    out_dir=None,
) -> dict:
    """Escalation-ladder stage of ``weekly_mf_holdings``: unbounded drain.

    Works EVERY remaining OPEN ticket, one §11.3 channel each via
    ``next_channel()`` (``amc_recheck -> web_search -> amfi -> advisorkhoj``
    -> ``manual``), oldest / lowest-coverage first. Unbounded on purpose: the
    ladder now runs once a week, AFTER the AMC agents and the audit have had
    their turn, and ``next_channel()`` keeps the source channels strictly
    downstream.
    """
    queue = _open_queue(queue_path, manual_csv_path)
    tickets = select_tickets(queue)
    if not tickets:
        return {"worked": 0, "closed": 0, "parked": 0, "still_open": 0}
    return _work_tickets(
        queue,
        tickets,
        _build_channel_runner(
            channel_runner=channel_runner, limiter=limiter, client=client, out_dir=out_dir
        ),
    )


def _render_digest(day_iso: str, rows: list, counts: dict[str, int]) -> str:
    """The markdown body: pending count, reason histogram, then the rows."""
    lines = [
        f"# Manual-intervention digest — {day_iso}",
        "",
        f"Pending rows: {len(rows)}",
        "",
        "## Pending rows by reason",
        "",
    ]
    if counts:
        for reason, n in sorted(counts.items()):
            lines.append(f"- {reason}: {n}")
    else:
        lines.append("- none (register is clear)")
    lines += ["", "## Pending rows", ""]
    if rows:
        lines += [
            "| queue_id | amc | scheme | month | coverage % | attempts "
            "| reason_code | first_seen |",
            "|---|---|---|---|---:|---:|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r.queue_id} | {r.amc} | {r.scheme} | {r.month} | "
                f"{r.coverage_pct:.2f} | {r.attempts} | {r.reason_code} "
                f"| {r.first_seen} |"
            )
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_manual_digest(
    *,
    report_dir: str | Path | None = None,
    manual_csv_path: str | Path | None = None,
    digest_date: date | str | None = None,
) -> dict:
    """``weekly_manual_digest``: markdown digest of the §11.4 manual register.

    Writes ``<report_dir>/manual_digest_<YYYY-MM-DD>.md`` from
    ``manual_register.pending_rows()`` plus ``counts_by_reason()`` - the
    human-readable view of every scheme-month parked to MANUAL that a human
    has not resolved yet (a human-set status stays authoritative, AC-20).
    The parent directory is created; a register with no pending rows still
    writes an explicit empty digest (never nothing), so the file's presence
    is itself the "register is clear" signal.
    """
    from src.agents import manual_register
    from src.agents.escalation import DEFAULT_MANUAL_CSV_PATH

    day = digest_date if digest_date is not None else date.today()
    day_iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    register_path = (
        manual_csv_path if manual_csv_path is not None else DEFAULT_MANUAL_CSV_PATH
    )
    rows = manual_register.pending_rows(register_path)
    counts = manual_register.counts_by_reason(register_path, pending_only=True)

    out_dir = Path(report_dir) if report_dir is not None else DEFAULT_REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"manual_digest_{day_iso}.md"
    path.write_text(_render_digest(day_iso, rows, counts), encoding="utf-8")
    logger.info("manual digest written: %s (%d pending rows)", path, len(rows))
    return {"path": str(path), "pending": len(rows), "counts": counts}


# The dispatch table: job id -> handler. Module-level function references, so
# a single handler can be patched/injected independently (tests use
# monkeypatch.setitem on this dict; the lookup happens per call). Keys must
# be exactly the ids in src.scheduler._AGENT_FLEET_JOBS.
JOB_HANDLERS: dict[str, Callable[..., dict]] = {
    JOB_INTEGRITY_AUDIT: run_integrity_audit,
    JOB_CORPORATE_ACTIONS: run_corporate_actions,
    JOB_MF_HOLDINGS: run_mf_holdings,
    JOB_MANUAL_DIGEST: write_manual_digest,
}


def agent_fleet_fn(job_name: str, **kwargs) -> dict:
    """THE entry point ``main.py`` passes to ``MonthlyScheduler(agent_fleet_fn=...)``.

    Called by the scheduler as ``agent_fleet_fn(job_name, **kwargs)`` (the
    per-job kwargs come from ``scheduler.agent_fleet_job_kwargs``);
    dispatches to the matching :data:`JOB_HANDLERS` handler and wraps its
    detail dict as ``{"job", "ok": True, "detail"}``. An UNKNOWN job name
    NEVER raises - it returns ``{"job", "ok": False, "error":
    "unknown_job"}`` so a config typo degrades to one logged failure instead
    of breaking the day's job. A handler that raises propagates: the
    scheduler's ``_run_agent_job`` wrapper contains and logs it, exactly like
    every other ``_run_X`` job wrapper.
    """
    handler = JOB_HANDLERS.get(str(job_name or ""))
    if handler is None:
        return {"job": job_name, "ok": False, "error": "unknown_job"}
    return {"job": job_name, "ok": True, "detail": handler(**kwargs)}
