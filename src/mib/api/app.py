"""FastAPI application factory.

Creating the app is a pure function — `main.py` composes it with
uvicorn + APScheduler + Telegram polling later on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mib import __version__
from mib.api.dependencies import shutdown_sources
from mib.api.routers import (
    ask,
    backtest,
    health,
    macro,
    news,
    portfolio,
    scan,
    symbol,
)
from mib.logger import logger
from mib.scheduler import register_bot_jobs, start_scheduler, stop_scheduler
from mib.telegram.bot import start_bot, stop_bot


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks — logged so we can trace boot order.

    Boot order is deliberate:
        1. ``start_scheduler()`` — registers the source health probe.
        2. ``start_bot()`` — polling starts in the same asyncio loop.
        3. ``register_bot_jobs()`` — 3 Telegram jobs can now call
           ``app.bot.send_message``.

    Shutdown runs in reverse so the bot stops accepting updates
    before the scheduler starts tearing down jobs.
    """
    logger.info("mib api starting · version={}", __version__)
    start_scheduler()
    await start_bot()
    register_bot_jobs()
    yield
    logger.info("mib api stopping")
    await stop_bot()
    stop_scheduler()
    await shutdown_sources()


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Market Intelligence Bot",
        description="Self-hosted financial intelligence API — local only.",
        version=__version__,
        # /docs and /redoc are still on by default. They're bound to
        # 127.0.0.1 (spec §13) so no LAN exposure; convenient during dev.
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(symbol.router)
    app.include_router(macro.router)
    app.include_router(news.router)
    app.include_router(ask.router)
    app.include_router(scan.router)
    app.include_router(portfolio.router)
    app.include_router(backtest.router)

    return app
