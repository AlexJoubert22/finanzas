"""FastAPI application factory.

Creating the app is a pure function — `main.py` composes it with
uvicorn + APScheduler + Telegram polling later on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mib import __version__
from mib.api.routers import health
from mib.logger import logger


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks — logged so we can trace boot order.

    The ``app`` argument is required by FastAPI's lifespan contract
    but unused here; prefixed with ``_`` so ruff's ARG lint is happy.
    """
    logger.info("mib api starting · version={}", __version__)
    yield
    logger.info("mib api stopping")


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
    # Additional routers (symbol, scan, news, macro, ask) land in phase 2+.

    return app
