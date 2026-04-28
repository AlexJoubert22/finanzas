"""Persistence boundary for :class:`mib.trading.signals.Signal`.

The repository owns the translation between the in-memory thesis
(``Signal``, frozen dataclass) and its DB row (``SignalRow`` ORM).
Callers outside this module never touch ``SignalRow`` directly — they
hand in a ``Signal`` to ``add()`` and receive ``PersistedSignal``
back from any read.

Mypy strict is intentionally NOT applied to this module: SQLAlchemy
``Mapped[T]`` inference and dynamic query building fight with strict
without catching proportional bugs. Strict lives on the pure
``mib.trading.signals`` types instead.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import SignalRow
from mib.logger import logger
from mib.trading.signals import (
    SIGNAL_STATUSES,
    PersistedSignal,
    Side,
    Signal,
    SignalStatus,
)


class SignalRepository:
    """CRUD for the ``signals`` table, dataclass-in / dataclass-out."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add(self, signal: Signal) -> PersistedSignal:
        """Persist ``signal`` as a new row with status='pending'."""
        async with self._sf() as session:
            row = _to_row(signal, status="pending", status_updated_at=signal.generated_at)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            logger.debug(
                "signal_repo: added id={} strategy={} ticker={}",
                row.id,
                row.strategy_id,
                row.ticker,
            )
            return _from_row(row)

    async def mark_status(
        self, signal_id: int, new_status: SignalStatus
    ) -> PersistedSignal | None:
        """Update ``status`` and ``status_updated_at``. Returns the new
        :class:`PersistedSignal`, or None if the id does not exist.
        """
        if new_status not in SIGNAL_STATUSES:
            raise ValueError(f"invalid SignalStatus: {new_status!r}")
        async with self._sf() as session:
            row = await session.get(SignalRow, signal_id)
            if row is None:
                return None
            row.status = new_status
            row.status_updated_at = datetime.now().astimezone()
            await session.commit()
            await session.refresh(row)
            return _from_row(row)

    # ─── Reads ─────────────────────────────────────────────────────

    async def get(self, signal_id: int) -> PersistedSignal | None:
        async with self._sf() as session:
            row = await session.get(SignalRow, signal_id)
            return _from_row(row) if row is not None else None

    async def list_pending(self, *, limit: int = 100) -> list[PersistedSignal]:
        async with self._sf() as session:
            stmt = (
                select(SignalRow)
                .where(SignalRow.status == "pending")
                .order_by(SignalRow.generated_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def list_by_strategy(
        self,
        strategy_id: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[PersistedSignal]:
        async with self._sf() as session:
            stmt = select(SignalRow).where(SignalRow.strategy_id == strategy_id)
            if since is not None:
                stmt = stmt.where(SignalRow.generated_at >= since)
            stmt = stmt.order_by(SignalRow.generated_at.desc()).limit(limit)
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]


# ─── Translation helpers ───────────────────────────────────────────

def _to_row(
    signal: Signal,
    *,
    status: SignalStatus,
    status_updated_at: datetime,
) -> SignalRow:
    low, high = signal.entry_zone
    # Cast indicators dict[str, float] → dict[str, Any] for the JSON
    # column; SQLAlchemy serialises through the standard json codec.
    indicators_payload: dict[str, object] = dict(signal.indicators)
    return SignalRow(
        ticker=signal.ticker,
        side=signal.side,
        strength=signal.strength,
        timeframe=signal.timeframe,
        entry_low=low,
        entry_high=high,
        invalidation=signal.invalidation,
        target_1=signal.target_1,
        target_2=signal.target_2,
        rationale=signal.rationale,
        indicators_json=indicators_payload,
        generated_at=signal.generated_at,
        strategy_id=signal.strategy_id,
        confidence_ai=signal.confidence_ai,
        status=status,
        status_updated_at=status_updated_at,
    )


def _from_row(row: SignalRow) -> PersistedSignal:
    raw_ind: dict[str, object] = row.indicators_json or {}
    indicators: dict[str, float] = {
        k: float(v) for k, v in raw_ind.items() if isinstance(v, (int, float))
    }
    side: Side = _coerce_side(row.side)
    status: SignalStatus = _coerce_status(row.status)
    signal = Signal(
        ticker=row.ticker,
        side=side,
        strength=row.strength,
        timeframe=row.timeframe,
        entry_zone=(row.entry_low, row.entry_high),
        invalidation=row.invalidation,
        target_1=row.target_1,
        target_2=row.target_2,
        rationale=row.rationale,
        indicators=indicators,
        generated_at=row.generated_at,
        strategy_id=row.strategy_id,
        confidence_ai=row.confidence_ai,
    )
    return PersistedSignal(
        id=row.id,
        status=status,
        signal=signal,
        status_updated_at=row.status_updated_at,
    )


def _coerce_side(raw: str) -> Side:
    if raw not in ("long", "short", "flat"):
        # Should never happen — DB check constraint filters this — but
        # the type system can't know that.
        raise ValueError(f"unexpected side in DB row: {raw!r}")
    return raw  # type: ignore[return-value]


def _coerce_status(raw: str) -> SignalStatus:
    if raw not in SIGNAL_STATUSES:
        raise ValueError(f"unexpected status in DB row: {raw!r}")
    return raw  # type: ignore[return-value]


_RECOGNISED_NUMERIC = (int, float)
"""Used in ``_from_row`` to filter the JSON payload back to numeric
indicator values. Anything non-numeric is dropped (the dataclass
contract is ``dict[str, float]``)."""
