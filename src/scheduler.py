from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# India has no DST: a fixed offset is exact year-round and avoids the
# Windows tzdata dependency that zoneinfo("Asia/Kolkata") would need.
IST = timezone(timedelta(hours=5, minutes=30))

# [BUG-M8] APScheduler's defaults drop runs whose scheduled moment is missed
# by more than misfire_grace_time=1 SECOND, and log-only. On a rebooting or
# busy host every job was one blocked event loop away from silently skipping
# a day. Long grace + coalesce makes a late wake-up run the job once.
_JOB_DEFAULTS = {"misfire_grace_time": 6 * 3600, "coalesce": True,
                 "max_instances": 1}

# Agent-fleet job registry — DAILY vs WEEKLY data policy (product owner):
#
# DAILY — market data only: mutual-fund NAV (daily_nav_refresh), stock daily
# closing prices (daily_stock_refresh), stock corporate actions
# (daily_corporate_actions — split out of the bundled stock job so an action
# refresh is never blocked by a price failure), bond prices
# (daily_bond_refresh) and the integrity audit (daily_integrity_audit —
# READ-ONLY: it audits stored data and fetches nothing, so it is compatible
# with "holdings are weekly"). The 21:45 corporate-actions slot is distinct
# from the 21:00 bundled stock job so the two never overlap.
#
# WEEKLY — mutual-fund holdings statement / portfolio breakup: ONE job,
# weekly_mf_holdings, performs the whole MF-holdings cycle in strict order —
# (a) the AMC discovery/download fleet FIRST (runner.run_all(production=True))
# so the AMC-own-website scrapers find and fetch the links, (b) then the
# integrity audit so the newly downloaded data is tiered, (c) ONLY THEN the
# escalation ladder, which per ticket walks amc_recheck -> web_search -> amfi
# -> advisorkhoj -> manual via next_channel() — i.e. AMFI/Advisorkhoj are
# reached ONLY as a downstream fallback after the AMC agents have had their
# turn. weekly_manual_digest stays weekly (reporting only, fetches nothing).
#
# daily_ladder_sweep and weekly_source_channels were removed as standalone
# jobs: mutual-fund holdings work is WEEKLY, and the source channels
# (AMFI/Advisorkhoj) must never be a parallel/peer activity — they are a
# fallback reached only after the AMC agents fail, which next_channel()
# ordering already guarantees. Their intent is preserved inside
# weekly_mf_holdings. Shape: (job_id, day_of_week or None for daily, default
# hour, default minute). All registered with the _JOB_DEFAULTS
# coalesce/max_instances guard and Asia/Kolkata triggers.
_AGENT_FLEET_JOBS: tuple[tuple[str, str | None, int, int], ...] = (
    ("daily_integrity_audit", None, 7, 20),
    ("daily_corporate_actions", None, 21, 45),
    ("weekly_mf_holdings", "mon", 6, 30),
    ("weekly_manual_digest", "fri", 18, 0),
)

_AGENT_JOB_NAMES = {
    "daily_integrity_audit": "Daily Integrity Audit",
    "daily_corporate_actions": "Daily Stock Corporate Actions",
    "weekly_mf_holdings": "Weekly MF Holdings Cycle",
    "weekly_manual_digest": "Weekly Manual-Register Digest",
}


def agent_fleet_job_kwargs(job_id: str, cfg: dict) -> dict:
    """Per-job kwargs forwarded to ``agent_fleet_fn`` (the dispatch contract).

    The weekly MF-holdings cycle runs its fleet stage with the real
    production bindings (``production=True``); the read-only audit, the
    corporate-actions refresh and the manual digest are kwargs-free. The
    former bounded ``daily_ladder_sweep`` cap is gone with that job — ladder
    work now happens once a week, unbounded, inside ``weekly_mf_holdings``.
    """
    if job_id == "weekly_mf_holdings":
        return {"production": True}
    return {}


def ist_now() -> datetime:
    return datetime.now(IST)


class MonthlyScheduler:
    def __init__(self, pipeline_fn, settings: dict, base_dir: Path,
                 nav_refresh_fn=None, stock_refresh_fn=None, bond_refresh_fn=None,
                 amfi_fn=None, preheal_fn=None, statements_fn=None,
                 otherdata_fn=None, agent_fleet_fn=None):
        self.pipeline_fn = pipeline_fn
        self.settings = settings
        self.base_dir = base_dir
        self.nav_refresh_fn = nav_refresh_fn
        self.stock_refresh_fn = stock_refresh_fn
        self.bond_refresh_fn = bond_refresh_fn
        self.amfi_fn = amfi_fn
        self.preheal_fn = preheal_fn
        self.statements_fn = statements_fn
        self.otherdata_fn = otherdata_fn
        # Single entry point for all agent-fleet jobs: called as
        # agent_fleet_fn(job_name, **kwargs) and dispatches on the job name
        # to the runner/integrity/escalation/manual_register components.
        self.agent_fleet_fn = agent_fleet_fn
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    def setup(self):
        sched = self.settings.get("scheduler", {})
        if not sched.get("enabled", True):
            logger.info("Scheduler disabled in config")
            return

        day = sched.get("day_of_month", 1)
        hour = sched.get("hour", 6)
        minute = sched.get("minute", 0)
        # Retry daily for the first N days of the month (disclosures may not be
        # published on day 1). Falls back to a single monthly run when <= 1.
        retry_days = int(sched.get("retry_days", 5))

        if retry_days > 1:
            day_expression = f"1-{retry_days}"
            logger.info(
                f"Scheduler set: daily on days {day_expression} at {hour:02d}:{minute:02d} IST"
            )
        else:
            day_expression = str(day)
            logger.info(f"Scheduler set: on day {day} at {hour:02d}:{minute:02d} IST")

        trigger = CronTrigger(
            day=day_expression,
            hour=hour,
            minute=minute,
            timezone="Asia/Kolkata",
        )

        self.scheduler.add_job(
            self._run_pipeline,
            trigger=trigger,
            id="monthly_holdings_fetch",
            name="Monthly MF Holdings Fetch",
            replace_existing=True,
            **_JOB_DEFAULTS,
        )

        # ---- Daily NAV refresh (keeps data/nav_history/*.json current) ----
        # Runs twice a day 12h apart (hour:minute + optional hour2:minute2)
        # so the latest NAV is never more than ~12h old. The AMFI tier-1
        # piggyback only rides the evening run (monthly data, no need x2).
        nav_cfg = sched.get("nav_refresh", {})
        if self.nav_refresh_fn and nav_cfg.get("enabled", True):
            nav_trigger = CronTrigger(
                hour=nav_cfg.get("hour", 20),
                minute=nav_cfg.get("minute", 30),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_nav_refresh,
                trigger=nav_trigger,
                id="daily_nav_refresh",
                name="Daily NAV Refresh",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Daily NAV refresh set: {nav_cfg.get('hour', 20):02d}:"
                f"{nav_cfg.get('minute', 30):02d} IST"
            )
            if nav_cfg.get("hour2") is not None:
                nav_trigger2 = CronTrigger(
                    hour=nav_cfg.get("hour2"),
                    minute=nav_cfg.get("minute2", nav_cfg.get("minute", 30)),
                    timezone="Asia/Kolkata",
                )
                self.scheduler.add_job(
                    self._run_nav_refresh,
                    trigger=nav_trigger2,
                    args=[False],   # morning run: skip the AMFI piggyback
                    id="daily_nav_refresh_2",
                    name="Daily NAV Refresh (2nd)",
                    replace_existing=True,
                    **_JOB_DEFAULTS,
                )
                logger.info(
                    f"Daily NAV refresh (2nd) set: {nav_cfg.get('hour2'):02d}:"
                    f"{nav_cfg.get('minute2', nav_cfg.get('minute', 30)):02d} IST"
                )

        # ---- Daily nav_history stub pre-heal [NAV-STUB] -------------------
        # Upgrades thin cold-start stubs from R2/mirror BEFORE the first
        # visitor pays the cost, bounded per run (once-per-code-per-process
        # guard inside makes repeats cheap no-ops).
        if self.preheal_fn:
            pre_cfg = sched.get("nav_preheal", {})
            if pre_cfg.get("enabled", True):
                pre_trigger = CronTrigger(
                    hour=pre_cfg.get("hour", 8),
                    minute=pre_cfg.get("minute", 35),
                    timezone="Asia/Kolkata",
                )
                self.scheduler.add_job(
                    self._run_preheal,
                    trigger=pre_trigger,
                    id="daily_nav_preheal",
                    name="Daily NAV Stub Pre-Heal",
                    replace_existing=True,
                    **_JOB_DEFAULTS,
                )
                logger.info(
                    f"Daily NAV stub pre-heal set: "
                    f"{pre_cfg.get('hour', 8):02d}:{pre_cfg.get('minute', 35):02d} IST"
                )

        # ---- Daily stock refresh (price + actions + reports) ----
        stock_cfg = sched.get("stock_refresh", {})
        if self.stock_refresh_fn and stock_cfg.get("enabled", True):
            stock_trigger = CronTrigger(
                hour=stock_cfg.get("hour", 21),
                minute=stock_cfg.get("minute", 0),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_stock_refresh,
                trigger=stock_trigger,
                id="daily_stock_refresh",
                name="Daily Stock Refresh",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Daily stock refresh set: {stock_cfg.get('hour', 21):02d}:"
                f"{stock_cfg.get('minute', 0):02d} IST"
            )

        # ---- Daily bond/debt-market refresh (bulk files + catalog) ----
        bond_cfg = sched.get("bond_refresh", {})
        if self.bond_refresh_fn and bond_cfg.get("enabled", True):
            bond_trigger = CronTrigger(
                hour=bond_cfg.get("hour", 21),
                minute=bond_cfg.get("minute", 30),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_bond_refresh,
                trigger=bond_trigger,
                id="daily_bond_refresh",
                name="Daily Bond/Debt Refresh",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Daily bond refresh set: {bond_cfg.get('hour', 21):02d}:"
                f"{bond_cfg.get('minute', 30):02d} IST"
            )

        # ---- Financial-statements refresh (AI extraction; stale-first) ----
        stmt_cfg = sched.get("statements_refresh", {})
        if self.statements_fn and stmt_cfg.get("enabled", True):
            stmt_trigger = CronTrigger(
                day_of_week=stmt_cfg.get("day_of_week", "mon"),
                hour=stmt_cfg.get("hour", 7),
                minute=stmt_cfg.get("minute", 0),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_statements,
                trigger=stmt_trigger,
                id="statements_refresh",
                name="Financial Statements Refresh (stale-first)",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Statements refresh set: {stmt_cfg.get('day_of_week', 'mon')} "
                f"{stmt_cfg.get('hour', 7):02d}:{stmt_cfg.get('minute', 0):02d} IST"
            )

        # ---- Monthly AMFI-disclosure fetch (registry verification; holdings
        # arrive via the AMC-website pipeline) [DATA-POLICY: AMFI/AMC/NSE] ----
        amfi_cfg = sched.get("amfi_refresh", {})
        if self.amfi_fn and amfi_cfg.get("enabled", True):
            amfi_trigger = CronTrigger(
                day="8-12",  # disclosures publish ~day 10; retry daily until done
                hour=amfi_cfg.get("hour", 7),
                minute=amfi_cfg.get("minute", 15),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_amfi,
                trigger=amfi_trigger,
                id="monthly_amfi_fetch",
                name="Monthly AMFI Disclosure Fetch",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Monthly AMFI fetch set: days 8-12 at {amfi_cfg.get('hour', 7):02d}:"
                f"{amfi_cfg.get('minute', 15):02d} IST"
            )

        # ---- Monthly AMFI other-data refresh (tracking error/difference,
        # scheme-wise disclosure, risk parameters, AUM, NFO) [PLAN_AMFI_DATA_SOURCES]
        od_cfg = sched.get("otherdata_refresh", {})
        if self.otherdata_fn and od_cfg.get("enabled", True):
            od_trigger = CronTrigger(
                day="8-12",  # same publish window as the disclosure feeds
                hour=od_cfg.get("hour", 7),
                minute=od_cfg.get("minute", 45),
                timezone="Asia/Kolkata",
            )
            self.scheduler.add_job(
                self._run_otherdata,
                trigger=od_trigger,
                id="monthly_amfi_otherdata",
                name="Monthly AMFI Other-Data Fetch",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
            logger.info(
                f"Monthly AMFI other-data fetch set: days 8-12 at "
                f"{od_cfg.get('hour', 7):02d}:{od_cfg.get('minute', 45):02d} IST"
            )

        # ---- AMC agent fleet (SPEC §8.1/§11 + the DAILY vs WEEKLY data
        # policy documented above _AGENT_FLEET_JOBS): the read-only integrity
        # audit, the corporate-actions refresh, the weekly MF-holdings cycle
        # (fleet -> audit -> ladder) and the manual digest, all dispatched
        # through the ONE injectable agent_fleet_fn(job_name, **kwargs) entry
        # point so the heavy agent imports stay out of the scheduler wiring.
        af_cfg = sched.get("agent_fleet", {})
        if self.agent_fleet_fn and af_cfg.get("enabled", True):
            for job_id, dow, d_hour, d_minute in _AGENT_FLEET_JOBS:
                job_cfg = af_cfg.get(job_id, {})
                if not job_cfg.get("enabled", True):
                    continue
                trigger_kwargs = {}
                if dow is not None:
                    trigger_kwargs["day_of_week"] = job_cfg.get("day_of_week", dow)
                af_trigger = CronTrigger(
                    hour=job_cfg.get("hour", d_hour),
                    minute=job_cfg.get("minute", d_minute),
                    timezone="Asia/Kolkata",
                    **trigger_kwargs,
                )
                self.scheduler.add_job(
                    self._run_agent_job,
                    trigger=af_trigger,
                    args=[job_id],
                    kwargs=agent_fleet_job_kwargs(job_id, af_cfg),
                    id=job_id,
                    name=_AGENT_JOB_NAMES[job_id],
                    replace_existing=True,
                    **_JOB_DEFAULTS,
                )
                logger.info(
                    f"Agent-fleet job {job_id} set: "
                    f"{job_cfg.get('day_of_week', dow or 'daily')} "
                    f"{job_cfg.get('hour', d_hour):02d}:"
                    f"{job_cfg.get('minute', d_minute):02d} IST"
                )

    async def _run_pipeline(self):
        # [BUG-M8] IST clock for target month/year AND marker naming: a UTC
        # host firing the 06:00 IST Aug-1 run at Jul-31 local previously
        # fetched July and wrote a July marker, skipping August.
        now = ist_now()
        marker = self.base_dir / "logs" / f"success_{now.year}-{now.month:02d}.marker"

        if marker.exists():
            logger.info(
                f"Already completed for {now.year}-{now.month:02d}, skipping retry"
            )
            return

        logger.info(f"Scheduler triggered at {now}")

        try:
            # pipeline_fn may be sync (webapp wiring) or async (CLI wiring);
            # run both correctly instead of awaiting a plain function [S2d].
            if inspect.iscoroutinefunction(self.pipeline_fn):
                await self.pipeline_fn(year=now.year, month=now.month)
            else:
                await asyncio.to_thread(self.pipeline_fn, year=now.year, month=now.month)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            logger.info("Scheduled pipeline completed successfully")
        except Exception as e:
            logger.error(f"Scheduled pipeline failed: {e}")

    async def _run_nav_refresh(self, piggyback_amfi: bool = True):
        logger.info("Daily NAV refresh triggered")
        # Piggyback: attempt the AMFI tier-1 holdings fetch first so it
        # populates the moment the provider recovers — never blocks NAVs.
        if self.amfi_fn and piggyback_amfi:
            try:
                amfi_summary = await asyncio.to_thread(self.amfi_fn)
                logger.info(f"AMFI piggyback fetch: {amfi_summary}")
            except Exception as e:
                logger.warning(f"AMFI piggyback fetch unavailable: {e}")
        days = int(self.settings.get("scheduler", {}).get("nav_refresh", {}).get("days", 10))
        try:
            summary = await asyncio.to_thread(self.nav_refresh_fn, days=days)
            logger.info(f"Daily NAV refresh complete: {summary}")
        except Exception as e:
            logger.error(f"Daily NAV refresh failed: {e}")

    async def _run_preheal(self):
        logger.info("Daily NAV stub pre-heal triggered")
        try:
            summary = await asyncio.to_thread(self.preheal_fn)
            logger.info(f"NAV stub pre-heal complete: {summary}")
        except Exception as e:
            logger.error(f"NAV stub pre-heal failed: {e}")

    async def _run_stock_refresh(self):
        logger.info("Daily stock refresh triggered")
        try:
            summary = await asyncio.to_thread(self.stock_refresh_fn)
            logger.info(f"Daily stock refresh complete: {summary}")
        except Exception as e:
            logger.error(f"Daily stock refresh failed: {e}")

    async def _run_bond_refresh(self):
        logger.info("Daily bond refresh triggered")
        try:
            summary = await asyncio.to_thread(self.bond_refresh_fn)
            logger.info(f"Daily bond refresh complete: {summary}")
        except Exception as e:
            logger.error(f"Daily bond refresh failed: {e}")

    async def _run_statements(self):
        logger.info("Statements refresh triggered")
        try:
            summary = await asyncio.to_thread(self.statements_fn)
            ok = sum(1 for r in (summary or []) if r.get("status") == "ok")
            logger.info(f"Statements refresh complete: {ok}/{len(summary or [])} ok")
        except Exception as e:
            logger.error(f"Statements refresh failed: {e}")

    async def _run_amfi(self):
        logger.info("Monthly AMFI fetch triggered")
        try:
            summary = await asyncio.to_thread(self.amfi_fn)
            logger.info(f"Monthly AMFI fetch complete: {summary}")
        except Exception as e:
            logger.error(f"Monthly AMFI fetch failed: {e}")

    async def _run_otherdata(self):
        logger.info("Monthly AMFI other-data fetch triggered")
        try:
            summary = await asyncio.to_thread(self.otherdata_fn)
            logger.info(f"Monthly AMFI other-data fetch complete: {summary}")
        except Exception as e:
            logger.error(f"Monthly AMFI other-data fetch failed: {e}")

    async def _run_agent_job(self, name: str, **kwargs):
        # Shared wrapper for all agent-fleet jobs: agent_fleet_fn
        # dispatches on `name` to the right component. One bad day must
        # never kill the scheduler, so the exception is logged and dropped
        # (same containment as every other _run_X wrapper).
        logger.info(f"Agent-fleet job {name} triggered")
        try:
            summary = await asyncio.to_thread(self.agent_fleet_fn, name, **kwargs)
            logger.info(f"Agent-fleet job {name} complete: {summary}")
        except Exception as e:
            logger.error(f"Agent-fleet job {name} failed: {e}")

    def start(self):
        self.setup()
        self.scheduler.start()
        self._record_heartbeat()
        logger.info("Scheduler started")

    def _record_heartbeat(self) -> None:
        """Telemetry heartbeat so 'never scheduled' is distinguishable from
        'never ran' [S2f]: surfaces in /api/admin/refresh-summary and the
        /api/health scheduler check. Must never break startup."""
        try:
            jobs = []
            for job in self.scheduler.get_jobs():
                nxt = job.next_run_time.isoformat(timespec="seconds") if job.next_run_time else None
                jobs.append({"id": job.id, "name": job.name, "next_run_at": nxt})
            from src.refresh_log import record
            record("scheduler", "alive", jobs=jobs,
                   next_wakeup=min((j["next_run_at"] for j in jobs
                                    if j["next_run_at"]), default=None))
        except Exception:
            logger.exception("scheduler heartbeat recording failed")

    def stop(self):
        self.scheduler.shutdown()
        logger.info("Scheduler stopped")

    def get_next_run(self) -> datetime | None:
        job = self.scheduler.get_job("monthly_holdings_fetch")
        if job and job.next_run_time:
            return job.next_run_time
        return None
