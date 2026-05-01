"""Graceful wind-down (FASE 14.5).

Two operator commands feed this module:

- ``/wind_down <reason>``: graceful exit. Disables new entries; lets
  existing positions close naturally on their native stops/targets.
  Records a row in ``wind_down_state`` for audit + monitor visibility.
- ``/shutdown <reason>``: same effect on entries. Differentiated only
  by the ``kind`` field in the audit row — the operator marks intent
  ("we're done", not "pause for the weekend").

Neither command force-closes positions. That's :func:`execute_panic`'s
job and only fires from ``/panic`` (FASE 13.6).

The :class:`WindDownService` is the only writer of
``wind_down_state``. The ``tick()`` helper, intended for a periodic
job (out of scope for 14.5 — left as a follow-up), refreshes the
``positions_remaining_last_check`` column and auto-flips
``completed_at`` when zero positions remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import WindDownStateRow
from mib.logger import logger
from mib.trading.risk.state import TradingStateService
from mib.trading.trade_repo import TradeRepository

#: Minimum reason length to avoid drive-by /wind_down or /shutdown.
MIN_WINDDOWN_REASON_LEN: int = 20

WindDownKind = Literal["wind_down", "shutdown"]


@dataclass(frozen=True)
class WindDownStartResult:
    """Outcome of :meth:`WindDownService.start`."""

    accepted: bool
    wind_down_id: int | None = None
    positions_at_start: int = 0
    reason: str | None = None
    """Why rejected (already_in_progress, reason_too_short)."""


@dataclass(frozen=True)
class WindDownTickResult:
    """One refresh of the active wind-down row."""

    positions_remaining: int
    completed: bool


class WindDownService:
    """Coordinator for the wind_down_state lifecycle."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        state_service: TradingStateService,
        trade_repo: TradeRepository,
    ) -> None:
        self._sf = session_factory
        self._state = state_service
        self._trades = trade_repo

    async def start(
        self,
        *,
        actor: str,
        reason: str,
        kind: WindDownKind = "wind_down",
    ) -> WindDownStartResult:
        cleaned = (reason or "").strip()
        if len(cleaned) < MIN_WINDDOWN_REASON_LEN:
            return WindDownStartResult(
                accepted=False,
                reason=(
                    f"reason_too_short:{len(cleaned)}_chars_need_"
                    f"{MIN_WINDDOWN_REASON_LEN}"
                ),
            )

        # Refuse if a wind-down is already in flight.
        active = await self.current()
        if active is not None and active.completed_at is None:
            return WindDownStartResult(
                accepted=False,
                wind_down_id=active.id,
                reason="already_in_progress",
            )

        open_trades = await self._trades.list_open()
        positions_at_start = len(open_trades)
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._sf() as session, session.begin():
            row = WindDownStateRow(
                started_at=now,
                started_by=f"{kind}:{actor}",
                completed_at=None,
                positions_at_start=positions_at_start,
                positions_remaining_last_check=positions_at_start,
                last_check_at=now,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)

        # Disable new entries via the canonical service so the audit
        # trail in trading_state stays consistent.
        await self._state.update(
            actor=f"{kind}:{actor} reason={cleaned[:80]!r}",
            enabled=False,
        )

        # If there were already zero positions, the wind-down completes
        # immediately — record it now so the operator gets a single
        # accurate signal.
        if positions_at_start == 0:
            await self._mark_completed(new_id, completed_at=now)

        logger.warning(
            "wind_down: started id={} kind={} actor={} positions={} reason={!r}",
            new_id, kind, actor, positions_at_start, cleaned,
        )
        return WindDownStartResult(
            accepted=True,
            wind_down_id=new_id,
            positions_at_start=positions_at_start,
        )

    async def current(self) -> WindDownStateRow | None:
        """Most recent row (any state). ``completed_at IS NULL`` means
        the wind-down is still in flight.
        """
        async with self._sf() as session:
            stmt = (
                select(WindDownStateRow)
                .order_by(WindDownStateRow.started_at.desc())
                .limit(1)
            )
            return (await session.scalars(stmt)).first()

    async def tick(self) -> WindDownTickResult | None:
        """Refresh the active row's ``positions_remaining_last_check``.

        Returns ``None`` when no wind-down is active (no row, or the
        latest row already has ``completed_at`` set). Auto-flips the
        active row to completed when zero open trades remain.
        """
        active = await self.current()
        if active is None or active.completed_at is not None:
            return None

        open_trades = await self._trades.list_open()
        remaining = len(open_trades)
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._sf() as session, session.begin():
            stmt = select(WindDownStateRow).where(
                WindDownStateRow.id == active.id
            )
            row = (await session.scalars(stmt)).first()
            if row is None:
                return None
            row.positions_remaining_last_check = remaining
            row.last_check_at = now
            if remaining == 0:
                row.completed_at = now

        completed = remaining == 0
        if completed:
            logger.warning(
                "wind_down: COMPLETED id={} all positions flat", active.id
            )
        return WindDownTickResult(
            positions_remaining=remaining,
            completed=completed,
        )

    # ─── Internal ─────────────────────────────────────────────────

    async def _mark_completed(
        self, wind_down_id: int, *, completed_at: datetime
    ) -> None:
        async with self._sf() as session, session.begin():
            stmt = select(WindDownStateRow).where(
                WindDownStateRow.id == wind_down_id
            )
            row = (await session.scalars(stmt)).first()
            if row is None:
                return
            row.completed_at = completed_at
            row.positions_remaining_last_check = 0
            row.last_check_at = completed_at
