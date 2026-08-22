from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class MonthlyScheduler:
    def __init__(self, pipeline_fn, settings: dict, base_dir: Path,
                 nav_refresh_fn=None, stock_refresh_fn=None, bond_refresh_fn=None,
                 amfi_fn=None):
        self.pipeline_fn = pipeline_fn
        self.settings = settings
        self.base_dir = base_dir
        self.nav_refresh_fn = nav_refresh_fn
        self.stock_refresh_fn = stock_refresh_fn
        self.bond_refresh_fn = bond_refresh_fn
        self.amfi_fn = amfi_fn
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
        )

        # ---- Daily NAV refresh (keeps data/nav_history/*.json current) ----
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
            )
            logger.info(
                f"Daily NAV refresh set: {nav_cfg.get('hour', 20):02d}:"
                f"{nav_cfg.get('minute', 30):02d} IST"
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
            )
            logger.info(
                f"Daily bond refresh set: {bond_cfg.get('hour', 21):02d}:"
                f"{bond_cfg.get('minute', 30):02d} IST"
            )

        # ---- Monthly AMFI/mfdata disclosure fetch (tier-1 holdings source) ----
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
            )
            logger.info(
                f"Monthly AMFI fetch set: days 8-12 at {amfi_cfg.get('hour', 7):02d}:"
                f"{amfi_cfg.get('minute', 15):02d} IST"
            )

    async def _run_pipeline(self):
        now = datetime.now()
        marker = self.base_dir / "logs" / f"success_{now.year}-{now.month:02d}.marker"

        if marker.exists():
            logger.info(
                f"Already completed for {now.year}-{now.month:02d}, skipping retry"
            )
            return

        logger.info(f"Scheduler triggered at {now}")

        try:
            await self.pipeline_fn(year=now.year, month=now.month)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            logger.info("Scheduled pipeline completed successfully")
        except Exception as e:
            logger.error(f"Scheduled pipeline failed: {e}")

    async def _run_nav_refresh(self):
        logger.info("Daily NAV refresh triggered")
        days = int(self.settings.get("scheduler", {}).get("nav_refresh", {}).get("days", 10))
        try:
            # The AMFI fetch is blocking (urllib) — run it in a worker thread so
            # the scheduler's event loop is never blocked.
            summary = await asyncio.to_thread(self.nav_refresh_fn, days=days)
            logger.info(f"Daily NAV refresh complete: {summary}")
        except Exception as e:
            logger.error(f"Daily NAV refresh failed: {e}")

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

    async def _run_amfi(self):
        logger.info("Monthly AMFI fetch triggered")
        try:
            summary = await asyncio.to_thread(self.amfi_fn)
            logger.info(f"Monthly AMFI fetch complete: {summary}")
        except Exception as e:
            logger.error(f"Monthly AMFI fetch failed: {e}")

    def start(self):
        self.setup()
        self.scheduler.start()
        logger.info("Scheduler started")

    def stop(self):
        self.scheduler.shutdown()
        logger.info("Scheduler stopped")

    def get_next_run(self) -> datetime | None:
        job = self.scheduler.get_job("monthly_holdings_fetch")
        if job and job.next_run_time:
            return job.next_run_time
        return None
