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
from mib.api.routers import ask, health, macro, news, scan, symbol
from mib.logger import logger
from mib.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks — logged so we can trace boot order.

    The ``app`` argument is required by FastAPI's lifespan contract
    but unused here; prefixed with ``_`` so ruff's ARG lint is happy.
    """
    logger.info("mib api starting · version={}", __version__)
    start_scheduler()
    yield
    logger.info("mib api stopping")
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

    return app
