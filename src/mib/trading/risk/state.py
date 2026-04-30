"""Read/write helper for the ``trading_state`` singleton row.

The DB enforces the singleton via ``CHECK (id = 1)``. This module is
the only place that mutates the row; gates only read. Each mutation
records ``last_modified_by`` (the actor) so the audit trail tells us
who flipped the kill switch and when.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import TradingState
from mib.logger import logger


@dataclass(frozen=True)
class TradingStateSnapshot:
    """Immutable view of ``trading_state`` at read time."""

    enabled: bool
    daily_dd_max_pct: float
    total_dd_max_pct: float
    killed_until: datetime | None
    last_modified_at: datetime
    last_modified_by: str


_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"enabled", "daily_dd_max_pct", "total_dd_max_pct", "killed_until"}
)


class TradingStateService:
    """Async service over the ``trading_state`` row.

    Construct with the global ``async_session_factory`` so reads and
    writes share the same engine (and therefore the same WAL).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self) -> TradingStateSnapshot:
        """Return the current state. Raises if the singleton is missing."""
        async with self._sf() as session:
            row = await session.get(TradingState, 1)
            if row is None:
                raise RuntimeError(
                    "trading_state singleton row (id=1) is missing — "
                    "did the seed migration run?"
                )
            return _to_snapshot(row)

    async def update(
        self, *, actor: str, **changes: Any
    ) -> TradingStateSnapshot:
        """Mutate a subset of fields, stamp ``last_modified_*``.

        Allowed keys: ``enabled``, ``daily_dd_max_pct``,
        ``total_dd_max_pct``, ``killed_until``. Any other key raises
        :class:`ValueError` so typos surface immediately.
        """
        unknown = set(changes) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"unknown trading_state keys: {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}"
            )
        if not actor:
            raise ValueError("actor must be a non-empty audit string")

        async with self._sf() as session:
            async with session.begin():
                row = await session.get(TradingState, 1)
                if row is None:
                    raise RuntimeError(
                        "trading_state singleton row (id=1) is missing"
                    )
                for key, value in changes.items():
                    setattr(row, key, value)
                row.last_modified_at = datetime.now(UTC)
                row.last_modified_by = actor
            await session.refresh(row)
            logger.info(
                "trading_state: actor={} changed={}",
                actor,
                sorted(changes.keys()),
            )
            return _to_snapshot(row)


def _to_snapshot(row: TradingState) -> TradingStateSnapshot:
    return TradingStateSnapshot(
        enabled=row.enabled,
        daily_dd_max_pct=row.daily_dd_max_pct,
        total_dd_max_pct=row.total_dd_max_pct,
        killed_until=row.killed_until,
        last_modified_at=row.last_modified_at,
        last_modified_by=row.last_modified_by,
    )
