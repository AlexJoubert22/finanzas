"""Persistent cache with TTL backed by the SQLite ``cache`` table.

Used by DataSources to avoid re-hitting upstream APIs within their
configured TTL. Serialisation is JSON for dicts/lists and raw bytes
for already-serialised payloads.

Usage:
    store = CacheStore()
    value = await store.get_or_set("ccxt:ticker:BTC/USDT", ttl=30,
                                   loader=lambda: fetch_from_ccxt())
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mib.db.models import Cache
from mib.db.session import async_session_factory
from mib.logger import logger

# Sentinel for "not cached / expired"
_MISS: Any = object()


class CacheStore[T]:
    """Async TTL cache persisted to SQLite.

    Keys are arbitrary UTF-8 strings (convention: ``<source>:<kind>:<arg>``).
    Values are stored as JSON-encoded bytes — caller is responsible for
    providing a JSON-serialisable object.
    """

    def __init__(self, source: str) -> None:
        """Create a scoped store for a given source (used for metrics).

        Args:
            source: Short identifier of the caller (``"ccxt"``, ``"yfinance"``,
                etc). Stored in the ``source`` column for observability.
        """
        self._source = source

    async def get(self, key: str) -> Any:
        """Return the cached value if present and not expired, else sentinel _MISS."""
        async with async_session_factory() as session:
            stmt = select(Cache).where(Cache.key == key)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return _MISS
            if row.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
                # Expired — drop it lazily (no explicit TTL sweeper needed).
                await session.execute(delete(Cache).where(Cache.key == key))
                await session.commit()
                return _MISS
            try:
                return json.loads(row.value)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("cache: failed to decode key={}: {}", key, exc)
                return _MISS

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store ``value`` under ``key`` for ``ttl_seconds`` seconds."""
        payload = json.dumps(value, separators=(",", ":"), default=str).encode()
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        # UPSERT on the primary key (SQLite 3.24+ supports ON CONFLICT).
        async with async_session_factory() as session:
            stmt = sqlite_insert(Cache).values(
                key=key,
                value=payload,
                expires_at=expires_at,
                source=self._source,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Cache.key],
                set_={
                    "value": stmt.excluded.value,
                    "expires_at": stmt.excluded.expires_at,
                    "source": stmt.excluded.source,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def get_or_set(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Return cached value if fresh; else call ``loader`` and cache result.

        Returns a tuple ``(value, hit)`` where ``hit`` is True when the value
        was served from cache.
        """
        cached = await self.get(key)
        if cached is not _MISS:
            return cached, True
        value = await loader()
        await self.set(key, value, ttl_seconds)
        return value, False

    async def delete(self, key: str) -> None:
        """Best-effort delete for a single key (noop if missing)."""
        async with async_session_factory() as session:
            await session.execute(delete(Cache).where(Cache.key == key))
            await session.commit()
