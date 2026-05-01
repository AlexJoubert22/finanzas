"""Scheduler job: refresh the in-memory portfolio cache every 30 s.

Runs in the same APScheduler used by the rest of the bot. Idempotent
by construction — ``PortfolioState.refresh`` overwrites the cache
atomically; failed fetches log a warning and leave the previous
cache in place so consumers continue getting the last good snapshot
rather than crashing the gate evaluations.
"""

from __future__ import annotations

import time

from mib.api.dependencies import get_portfolio_state
from mib.logger import logger
from mib.observability.scheduler_health import get_scheduler_health


async def portfolio_sync_job() -> None:
    """One tick of the portfolio refresh loop.

    Logged metrics: equity (in quote currency), open position count,
    sync latency in ms. The job never raises — APScheduler keeps
    ticking even if a single fetch fails.
    """
    # Heartbeat scheduler liveness even if the actual fetch fails —
    # the scheduler ITSELF is alive as long as this function runs.
    get_scheduler_health().mark_tick()
    state = get_portfolio_state()
    started = time.monotonic()
    try:
        snapshot = await state.refresh()
    except Exception as exc:  # noqa: BLE001 — never crash the scheduler
        logger.warning("portfolio_sync: refresh failed: {}", exc)
        return

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "portfolio_sync: source={} equity={} positions={} latency_ms={}",
        snapshot.source,
        snapshot.equity_quote,
        len(snapshot.positions),
        elapsed_ms,
    )
