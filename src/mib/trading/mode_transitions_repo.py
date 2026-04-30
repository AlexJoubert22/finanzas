"""Append-only repository for ``mode_transitions`` (FASE 10.2).

INSERT-only by contract — no method here ever issues UPDATE or DELETE.
The temporal guards (FASE 10.3) and the ``/mode_status`` projection
(FASE 10.4) read this table; nothing else mutates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import ModeTransitionRow
from mib.logger import logger
from mib.trading.mode import TradingMode


@dataclass(frozen=True)
class ModeTransition:
    """In-memory view of a row in ``mode_transitions``."""

    id: int
    from_mode: TradingMode
    to_mode: TradingMode
    actor: str
    reason: str | None
    transitioned_at: datetime
    override_used: bool
    mode_started_at_after_transition: datetime


class ModeTransitionRepository:
    """INSERT-only persistence boundary for ``mode_transitions``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add(
        self,
        *,
        from_mode: TradingMode,
        to_mode: TradingMode,
        actor: str,
        reason: str | None,
        transitioned_at: datetime,
        override_used: bool,
        mode_started_at_after_transition: datetime,
    ) -> int:
        """Append a new transition row. Returns its primary key."""
        async with self._sf() as session, session.begin():
            row = ModeTransitionRow(
                from_mode=from_mode.value,
                to_mode=to_mode.value,
                actor=actor,
                reason=reason,
                transitioned_at=transitioned_at,
                override_used=override_used,
                mode_started_at_after_transition=(
                    mode_started_at_after_transition
                ),
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
        logger.debug(
            "mode_transitions: added id={} {} -> {} actor={} override={}",
            new_id,
            from_mode,
            to_mode,
            actor,
            override_used,
        )
        return new_id

    # ─── Reads ─────────────────────────────────────────────────────

    async def latest(self) -> ModeTransition | None:
        """Most recent transition (any mode)."""
        async with self._sf() as session:
            stmt = (
                select(ModeTransitionRow)
                .order_by(ModeTransitionRow.transitioned_at.desc())
                .limit(1)
            )
            row = (await session.scalars(stmt)).first()
            return _to_dc(row) if row is not None else None

    async def latest_into(
        self, mode: TradingMode
    ) -> ModeTransition | None:
        """Most recent transition INTO ``mode`` — anchors "days in mode" guards."""
        async with self._sf() as session:
            stmt = (
                select(ModeTransitionRow)
                .where(ModeTransitionRow.to_mode == mode.value)
                .order_by(ModeTransitionRow.transitioned_at.desc())
                .limit(1)
            )
            row = (await session.scalars(stmt)).first()
            return _to_dc(row) if row is not None else None

    async def list_recent(self, *, limit: int = 50) -> list[ModeTransition]:
        async with self._sf() as session:
            stmt = (
                select(ModeTransitionRow)
                .order_by(ModeTransitionRow.transitioned_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_to_dc(r) for r in rows]

    async def list_forces_in_window(
        self, *, actor: str, since: datetime
    ) -> list[ModeTransition]:
        """All ``override_used=True`` transitions by ``actor`` since
        ``since``. Used by FASE 10.5 to enforce 1-force-per-week.
        """
        async with self._sf() as session:
            stmt = (
                select(ModeTransitionRow)
                .where(
                    ModeTransitionRow.actor == actor,
                    ModeTransitionRow.override_used.is_(True),
                    ModeTransitionRow.transitioned_at >= since,
                )
                .order_by(ModeTransitionRow.transitioned_at.desc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_to_dc(r) for r in rows]


def _to_dc(row: ModeTransitionRow) -> ModeTransition:
    return ModeTransition(
        id=row.id,
        from_mode=TradingMode(row.from_mode),
        to_mode=TradingMode(row.to_mode),
        actor=row.actor,
        reason=row.reason,
        transitioned_at=row.transitioned_at,
        override_used=row.override_used,
        mode_started_at_after_transition=row.mode_started_at_after_transition,
    )
