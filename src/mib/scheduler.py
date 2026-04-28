"""APScheduler wiring — single async scheduler shared by all phases.

Phase 2 uses it only for the source-health probe every 5 minutes.
Later phases (price alerts, watchlist monitor, news monitor) plug their
own jobs here. Keeping a single process-wide scheduler ensures we don't
spawn duplicate jobs if app factory is called twice in tests.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mib.api.dependencies import (
    get_ccxt_source,
    get_coingecko_source,
    get_finnhub_source,
    get_fred_source,
    get_rss_source,
    get_tradingview_source,
    get_yfinance_source,
)
from mib.config import get_settings
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
        get_coingecko_source(),
        get_finnhub_source(),
        get_fred_source(),
        get_rss_source(),
        # AlphaVantage is *not* in the probe: each probe burns 1 of our
        # 25/day calls. Its status is derived lazily from actual usage.
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

    # FASE 8.1 — TTL expiration of pending signals every 15 min.
    # IntervalTrigger (NOT cron) because the cadence is not anchored
    # to candle close; the job just sweeps DB state.
    from mib.trading.expiration import expire_stale_signals_job  # noqa: PLC0415

    sched.add_job(
        expire_stale_signals_job,
        trigger=IntervalTrigger(minutes=15),
        id="expire_stale_signals",
        name="Expire pending signals past TTL",
        replace_existing=True,
    )

    sched.start()
    # Fire once ASAP to populate the cache; fire-and-forget.
    import asyncio

    asyncio.create_task(_probe_sources_job())
    logger.info(
        "scheduler: started with health probe (5min) + expire_stale_signals (15min)"
    )


def register_bot_jobs() -> None:
    """Attach the 3 Telegram-bot background jobs. Called after the bot starts.

    Idempotent — ``replace_existing=True`` avoids duplicates if the
    lifespan cycles (e.g. test harness creating the app twice).
    """
    from mib.telegram.bot import get_bot_app  # noqa: PLC0415 - avoid circular import
    from mib.telegram.jobs.news_monitor import run_news_monitor_job  # noqa: PLC0415
    from mib.telegram.jobs.price_alerts import run_price_alerts_job  # noqa: PLC0415
    from mib.telegram.jobs.watchlist_monitor import (  # noqa: PLC0415
        run_watchlist_monitor_job,
    )

    bot_app = get_bot_app()
    if bot_app is None:
        logger.info("scheduler: bot not running — skipping bot jobs")
        return

    sched = get_scheduler()
    settings = get_settings()

    # Price alerts — 60 s default.
    sched.add_job(
        run_price_alerts_job,
        trigger=IntervalTrigger(seconds=settings.price_alerts_interval_sec),
        id="bot_price_alerts",
        name="Price alerts scan",
        args=[bot_app],
        replace_existing=True,
    )
    # Watchlist monitor — 5 min default.
    sched.add_job(
        run_watchlist_monitor_job,
        trigger=IntervalTrigger(seconds=settings.watchlist_interval_sec),
        id="bot_watchlist_monitor",
        name="Watchlist anomaly monitor",
        args=[bot_app],
        replace_existing=True,
    )
    # News monitor — 15 min default.
    sched.add_job(
        run_news_monitor_job,
        trigger=IntervalTrigger(seconds=settings.news_monitor_interval_sec),
        id="bot_news_monitor",
        name="News monitor for watchlists",
        args=[bot_app],
        replace_existing=True,
    )
    logger.info(
        "scheduler: bot jobs registered (price={}s watchlist={}s news={}s)",
        settings.price_alerts_interval_sec,
        settings.watchlist_interval_sec,
        settings.news_monitor_interval_sec,
    )


def stop_scheduler() -> None:
    """Shut down the scheduler cleanly. Idempotent."""
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("scheduler: stopped")
