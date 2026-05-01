"""Scheduler job: News Reactor every 5 min (FASE 11.3).

Mirrors the other periodic trading jobs: never raises, swallows
exceptions to keep the scheduler ticking.
"""

from __future__ import annotations

from mib.api.dependencies import get_news_reactor
from mib.logger import logger


async def news_reactor_job() -> None:
    reactor = get_news_reactor()
    try:
        proposals = await reactor.run_once()
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_reactor_job: unexpected failure: {}", exc)
        return
    logger.debug(
        "news_reactor_job: emitted {} proposal(s)", len(proposals)
    )
