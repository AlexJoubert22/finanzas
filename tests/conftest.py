"""Shared pytest fixtures.

Tests run against a **fresh temp-file SQLite** per pytest session. We
have to patch ``os.environ`` and wipe Pydantic's settings cache *before*
any ``mib.*`` import that could read the real ``.env`` file.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

# Patch env as early as possible — at module import time — so any indirect
# ``from mib.config import ...`` from another test module's import gets
# our values rather than the project's ``.env``.
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="mib-tests-"))
_TEST_DB = _TEST_DB_DIR / "test.db"

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_ALLOWED_USERS"] = "1"
os.environ["API_HOST"] = "127.0.0.1"

# Make sure the cached `Settings()` singleton picks our values up. Pydantic
# may have already been imported as a side-effect of test collection, so
# clear the cache explicitly.
from mib.config import get_settings  # noqa: E402

get_settings.cache_clear()
_settings = get_settings()
assert _settings.database_url.endswith("test.db"), (
    f"tests must use the scratch DB, got {_settings.database_url}"
)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db() -> Iterator[None]:
    """Remove the scratch DB file when the test session finishes."""
    yield
    for path in _TEST_DB_DIR.glob("*"):
        try:
            path.unlink()
        except OSError:  # pragma: no cover — best-effort cleanup
            pass
    try:
        _TEST_DB_DIR.rmdir()
    except OSError:  # pragma: no cover
        pass


@pytest_asyncio.fixture(scope="function")
async def fresh_db() -> AsyncIterator[None]:
    """Drop and re-create all tables on the scratch DB for each test."""
    # Late import: models register on Base.metadata when first imported.
    import mib.db.models  # noqa: F401
    from mib.db.session import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
