"""Async SQLAlchemy engine + session factory.

Per spec §7: SQLite with WAL journal, NORMAL synchronous, foreign keys ON,
and temp_store in memory. Pragmas are set on every new connection via
the `connect` event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from mib.config import get_settings
from mib.logger import logger

_settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Echo only if explicitly in debug mode — never in production.
_echo = _settings.log_level == "DEBUG" and _settings.app_env != "production"

engine = create_async_engine(
    _settings.database_url,
    echo=_echo,
    future=True,
    # Single connection is enough for a single-user bot (spec §11bis).
    # We rely on SQLite's serialisation and WAL for concurrency.
    connect_args={"timeout": 30},
)

async_session_factory = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


# ─── Pragmas enforced on every new SQLite connection ───────────────────
@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
    """Apply the pragmas listed in spec §7 every time a connection opens."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA temp_store = MEMORY")
    cursor.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a short-lived session."""
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    """Create tables if they do not yet exist.

    In production Alembic manages the schema; this helper is kept only
    for the dev path (`APP_ENV=development`) and tests.
    """
    if _settings.is_production:
        logger.info("init_db skipped (production uses Alembic)")
        return
    async with engine.begin() as conn:
        # Import models so they register on Base.metadata
        from mib.db import models  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    logger.info("init_db complete (dev mode)")
