"""Shared pytest fixtures.

Every test runs against an **in-memory SQLite** (``:memory:``) so they
stay isolated and hermetic. The real DB on disk is never touched.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio


@pytest.fixture(scope="session", autouse=True)
def _configure_env() -> None:
    """Force test env before any mib.* import that reads settings."""
    # Override settings at session level so `from mib.config import Settings()`
    # returns values suitable for tests.
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    os.environ.setdefault("TELEGRAM_ALLOWED_USERS", "1")
    os.environ.setdefault("API_HOST", "127.0.0.1")


@pytest_asyncio.fixture(scope="function")
async def fresh_db() -> AsyncIterator[None]:
    """Re-create tables on the in-memory DB for each test that needs them."""
    # Late import so _configure_env runs first.
    # Importing models registers them on Base.metadata.
    import mib.db.models  # noqa: F401
    from mib.db.session import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
