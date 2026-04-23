"""APScheduler wiring — single async scheduler shared by all phases.

Phase 2 uses it only for the source-health probe every 5 minutes.
Later phases (price alerts, watchlist monitor, news monitor) plug their
own jobs here. Keeping a single process-wide scheduler ensures we don't
spawn duplicate jobs if app factory is called twice in tests.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from mib.api.dependencies import (
    get_ccxt_source,
    get_tradingview_source,
    get_yfinance_source,
)
from mib.logger import logger
from mib.services.health_probe import get_health_cache

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler  # noqa: PLW0603 - intentional singleton
    if _scheduler is None:
        # max_instances=1 per spec §11bis — avoids overlapping runs
        # when a job is slower than its interval (shouldn't happen, but
        # defensive against RAM spikes).
        _scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
    return _scheduler


async def _probe_sources_job() -> None:
    """Job: probe every DataSource and cache the result."""
    cache = get_health_cache()
    sources = [
        get_ccxt_source(),
        get_yfinance_source(),
        get_tradingview_source(),
    ]
    await cache.probe_all(sources)
    logger.debug("health-probe: cache={}", cache.snapshot())


def start_scheduler() -> None:
    """Register jobs and start the scheduler. Idempotent."""
    sched = get_scheduler()
    if sched.running:
        return

    # Source health probe — runs immediately once, then every 5 min.
    sched.add_job(
        _probe_sources_job,
        trigger=IntervalTrigger(minutes=5),
        id="health_probe_sources",
        name="Probe DataSources for /health",
        replace_existing=True,
        next_run_time=None,  # we kick it manually below to seed the cache fast
    )
    sched.start()
    # Fire once ASAP to populate the cache; fire-and-forget.
    import asyncio

    asyncio.create_task(_probe_sources_job())
    logger.info("scheduler: started with health probe job (5-min interval)")


def stop_scheduler() -> None:
    """Shut down the scheduler cleanly. Idempotent."""
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("scheduler: stopped")
