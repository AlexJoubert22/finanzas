"""Append-only repository for the ``trades`` table (FASE 9.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import OrderRow, TradeRow, TradeStatusEvent
from mib.logger import logger
from mib.trading.trades import (
    TRADE_STATUSES,
    Trade,
    TradeEventType,
    TradeInputs,
    TradeSide,
    TradeStatus,
)


class TradeStaleStateError(Exception):
    """``transition`` saw a different ``from_status`` than expected."""

    def __init__(self, trade_id: int, expected: str, actual: str) -> None:
        super().__init__(
            f"trade #{trade_id}: expected from_status={expected!r}, got {actual!r}"
        )
        self.trade_id = trade_id
        self.expected = expected
        self.actual = actual


class TradeAlreadyExistsError(Exception):
    """One trade per signal — UNIQUE(signal_id) blew on insert."""

    def __init__(self, signal_id: int) -> None:
        super().__init__(f"trade for signal #{signal_id} already exists")
        self.signal_id = signal_id


class TradeRepository:
    """CRUD for ``trades``, dataclass-in / dataclass-out, append-only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add(self, inputs: TradeInputs) -> Trade:
        """Insert a ``pending`` trade + ``created`` event in one tx."""
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._sf() as session:
            row = TradeRow(
                signal_id=inputs.signal_id,
                ticker=inputs.ticker,
                side=inputs.side,
                size=inputs.size,
                entry_price=inputs.entry_price,
                stop_loss_price=inputs.stop_loss_price,
                take_profit_price=inputs.take_profit_price,
                opened_at=now,
                closed_at=None,
                status="pending",
                realized_pnl_quote=None,
                fees_paid_quote=Decimal(0),
                exchange_id=inputs.exchange_id,
                metadata_json=dict(inputs.metadata) if inputs.metadata else None,
            )
            session.add(row)
            try:
                async with session.begin_nested():
                    await session.flush()
                    event = TradeStatusEvent(
                        trade_id=row.id,
                        from_status=None,
                        to_status="pending",
                        event_type="created",
                        actor="system",
                        reason=None,
                        metadata_json=None,
                    )
                    session.add(event)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                logger.info(
                    "trade_repo: UNIQUE(signal_id) collision for {}: {}",
                    inputs.signal_id,
                    exc,
                )
                raise TradeAlreadyExistsError(inputs.signal_id) from exc
            await session.refresh(row)
            return _row_to_trade(row)

    async def transition(
        self,
        trade_id: int,
        to_status: TradeStatus,
        *,
        actor: str,
        event_type: TradeEventType,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_from_status: TradeStatus | None = None,
        exit_price: Decimal | None = None,
        realized_pnl_quote: Decimal | None = None,
        fees_increment: Decimal | None = None,
    ) -> Trade | None:
        """Atomic transition + audit. Optional kwargs apply structural
        updates inside the same transaction:

        - ``exit_price`` / ``realized_pnl_quote`` / ``fees_increment``
          flow into the row when transitioning to ``closed`` so the
          consumer doesn't need a follow-up UPDATE.
        - ``closed_at`` auto-populates on closed/failed.
        """
        if to_status not in TRADE_STATUSES:
            raise ValueError(f"invalid TradeStatus: {to_status!r}")
        if (
            expected_from_status is not None
            and expected_from_status not in TRADE_STATUSES
        ):
            raise ValueError(
                f"invalid expected TradeStatus: {expected_from_status!r}"
            )

        async with self._sf() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                row = await session.get(TradeRow, trade_id)
                if row is None:
                    await session.rollback()
                    return None

                current = row.status
                if (
                    expected_from_status is not None
                    and current != expected_from_status
                ):
                    await session.rollback()
                    raise TradeStaleStateError(
                        trade_id, expected_from_status, current
                    )

                now = datetime.now(UTC).replace(tzinfo=None)
                event = TradeStatusEvent(
                    trade_id=trade_id,
                    from_status=current,
                    to_status=to_status,
                    event_type=event_type,
                    actor=actor,
                    reason=reason,
                    metadata_json=metadata,
                )
                session.add(event)
                row.status = to_status

                if to_status in ("closed", "failed") and row.closed_at is None:
                    row.closed_at = now
                if exit_price is not None:
                    row.exit_price = exit_price
                if realized_pnl_quote is not None:
                    row.realized_pnl_quote = realized_pnl_quote
                if fees_increment is not None:
                    row.fees_paid_quote = (
                        row.fees_paid_quote + fees_increment
                    )

                await session.commit()
            except TradeStaleStateError:
                raise
            except Exception:
                await session.rollback()
                raise

            await session.refresh(row)
            logger.debug(
                "trade_repo: transitioned id={} {}->{} actor={} event={}",
                trade_id,
                current,
                to_status,
                actor,
                event_type,
            )
            return _row_to_trade(row)

    async def link_orders_to_trade(
        self, trade_id: int, order_ids: list[int]
    ) -> None:
        """Backpopulate ``orders.trade_id`` for every order in the list.

        This is a structural update (FK linkage), NOT a status
        change, so no events are written. The trade's status stays
        whatever it was; the executor calls this once at trade-open
        time so the entry + stop both point at the trade.
        """
        if not order_ids:
            return
        async with self._sf() as session, session.begin():
            for oid in order_ids:
                row = await session.get(OrderRow, oid)
                if row is None:
                    raise ValueError(f"order #{oid} not found for trade #{trade_id}")
                row.trade_id = trade_id

    # ─── Reads ─────────────────────────────────────────────────────

    async def get(self, trade_id: int) -> Trade | None:
        async with self._sf() as session:
            row = await session.get(TradeRow, trade_id)
            return _row_to_trade(row) if row is not None else None

    async def get_by_signal(self, signal_id: int) -> Trade | None:
        async with self._sf() as session:
            stmt = select(TradeRow).where(TradeRow.signal_id == signal_id)
            row = (await session.scalars(stmt)).first()
            return _row_to_trade(row) if row is not None else None

    async def list_open(self) -> list[Trade]:
        async with self._sf() as session:
            stmt = (
                select(TradeRow)
                .where(TradeRow.status.in_(("pending", "open")))
                .order_by(TradeRow.opened_at.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_row_to_trade(r) for r in rows]

    async def list_events(self, trade_id: int) -> list[TradeStatusEvent]:
        async with self._sf() as session:
            stmt = (
                select(TradeStatusEvent)
                .where(TradeStatusEvent.trade_id == trade_id)
                .order_by(
                    TradeStatusEvent.created_at.asc(),
                    TradeStatusEvent.id.asc(),
                )
            )
            rows = (await session.scalars(stmt)).all()
            return list(rows)


# ─── Translation ───────────────────────────────────────────────────

def _row_to_trade(row: TradeRow) -> Trade:
    side: TradeSide = _coerce_side(row.side)
    status: TradeStatus = _coerce_status(row.status)
    return Trade(
        trade_id=row.id,
        signal_id=row.signal_id,
        ticker=row.ticker,
        side=side,
        size=Decimal(str(row.size)),
        entry_price=Decimal(str(row.entry_price)),
        exit_price=(
            Decimal(str(row.exit_price)) if row.exit_price is not None else None
        ),
        stop_loss_price=Decimal(str(row.stop_loss_price)),
        take_profit_price=(
            Decimal(str(row.take_profit_price))
            if row.take_profit_price is not None
            else None
        ),
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        status=status,
        realized_pnl_quote=(
            Decimal(str(row.realized_pnl_quote))
            if row.realized_pnl_quote is not None
            else None
        ),
        fees_paid_quote=Decimal(str(row.fees_paid_quote)),
        exchange_id=row.exchange_id,
        metadata_json=row.metadata_json,
    )


def _coerce_side(raw: str) -> TradeSide:
    if raw not in ("long", "short"):
        raise ValueError(f"unexpected trade side in DB: {raw!r}")
    return raw  # type: ignore[return-value]


def _coerce_status(raw: str) -> TradeStatus:
    if raw not in TRADE_STATUSES:
        raise ValueError(f"unexpected trade status in DB: {raw!r}")
    return raw
