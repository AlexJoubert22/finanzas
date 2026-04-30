"""Append-only repository for the ``orders`` table (FASE 9.2).

Mirrors :class:`mib.trading.signal_repo.SignalRepository`:

- ``add()`` writes the row + a ``created`` event in one transaction.
- ``transition()`` writes the next event + updates ``orders.status``
  atomically with ``BEGIN IMMEDIATE`` to serialise concurrent writers.
- Direct ``UPDATE`` from business code is forbidden by convention.

Idempotency: callers request an order via :meth:`add_or_get`, which
derives a deterministic ``client_order_id`` from the
:class:`OrderInputs`. The first call inserts; concurrent or retry
calls hit the ``UNIQUE(client_order_id)`` constraint and return the
existing row's :class:`OrderResult`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import OrderRow, OrderStatusEvent
from mib.logger import logger
from mib.trading.orders import (
    ORDER_STATUSES,
    OrderEventType,
    OrderInputs,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)


class OrderStaleStateError(Exception):
    """``transition`` saw a different ``from_status`` than expected."""

    def __init__(self, order_id: int, expected: str, actual: str) -> None:
        super().__init__(
            f"order #{order_id}: expected from_status={expected!r}, got {actual!r}"
        )
        self.order_id = order_id
        self.expected = expected
        self.actual = actual


def derive_client_order_id(inputs: OrderInputs) -> str:
    """Deterministic ``mib-{signal_id}-{hash}`` id for idempotency.

    Same inputs → same id → UNIQUE constraint catches retries.
    Hash includes signal_id + side + type + amount + price + reduce_only
    so callers asking for materially different orders get fresh ids.
    The 8-byte hex tail keeps the total under Telegram callback limits
    even if we ever route order ids through chat ui.
    """
    parts = [
        str(inputs.signal_id),
        inputs.symbol,
        inputs.side,
        inputs.type,
        str(inputs.amount),
        str(inputs.price) if inputs.price is not None else "none",
        "1" if inputs.reduce_only else "0",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return f"mib-{inputs.signal_id}-{digest}"


class OrderRepository:
    """CRUD for ``orders``, dataclass-in / dataclass-out, append-only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add_or_get(
        self,
        inputs: OrderInputs,
        *,
        exchange_id: str,
        raw_payload: dict[str, Any],
    ) -> OrderResult:
        """Idempotent create. Returns existing row on duplicate.

        First caller:
            INSERT row with status='created' + 'created' event
            → returns OrderResult(status='created').

        Retry / concurrent caller:
            UNIQUE(client_order_id) blows → catch IntegrityError →
            fetch existing row → return its current OrderResult.
        """
        client_order_id = derive_client_order_id(inputs)
        existing = await self.get_by_client_order_id(client_order_id)
        if existing is not None:
            logger.debug(
                "order_repo: idempotent hit client_order_id={} status={}",
                client_order_id,
                existing.status,
            )
            return existing

        async with self._sf() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            row = OrderRow(
                trade_id=None,
                signal_id=inputs.signal_id,
                client_order_id=client_order_id,
                exchange_order_id=None,
                exchange_id=exchange_id,
                type=inputs.type,
                side=inputs.side,
                status="created",
                price=inputs.price,
                amount=inputs.amount,
                reduce_only=inputs.reduce_only,
                raw_payload_json=raw_payload,
                raw_response_json=None,
                created_at=now,
                submitted_at=None,
                filled_at=None,
            )
            session.add(row)
            try:
                async with session.begin_nested():
                    await session.flush()
                    event = OrderStatusEvent(
                        order_id=row.id,
                        from_status=None,
                        to_status="created",
                        event_type="created",
                        actor="system",
                        reason=None,
                        metadata_json=None,
                    )
                    session.add(event)
                await session.commit()
            except IntegrityError as exc:
                # Concurrent insert hit the same client_order_id —
                # surface the existing row.
                await session.rollback()
                logger.info(
                    "order_repo: concurrent insert collision for {}: {}",
                    client_order_id,
                    exc,
                )
                fallback = await self.get_by_client_order_id(client_order_id)
                if fallback is None:
                    raise
                return fallback

            await session.refresh(row)
            return _row_to_result(row)

    async def transition(
        self,
        order_id: int,
        to_status: OrderStatus,
        *,
        actor: str,
        event_type: OrderEventType,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_from_status: OrderStatus | None = None,
        exchange_order_id: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> OrderResult | None:
        """Atomic status transition + audit event.

        Optional kwargs ``exchange_order_id`` and ``raw_response`` are
        applied to the row in the same transaction (avoids a follow-up
        UPDATE).
        """
        if to_status not in ORDER_STATUSES:
            raise ValueError(f"invalid OrderStatus: {to_status!r}")
        if (
            expected_from_status is not None
            and expected_from_status not in ORDER_STATUSES
        ):
            raise ValueError(
                f"invalid expected OrderStatus: {expected_from_status!r}"
            )

        async with self._sf() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                row = await session.get(OrderRow, order_id)
                if row is None:
                    await session.rollback()
                    return None

                current = row.status
                if (
                    expected_from_status is not None
                    and current != expected_from_status
                ):
                    await session.rollback()
                    raise OrderStaleStateError(
                        order_id, expected_from_status, current
                    )

                now = datetime.now(UTC).replace(tzinfo=None)
                event = OrderStatusEvent(
                    order_id=order_id,
                    from_status=current,
                    to_status=to_status,
                    event_type=event_type,
                    actor=actor,
                    reason=reason,
                    metadata_json=metadata,
                )
                session.add(event)
                row.status = to_status
                if to_status == "submitted" and row.submitted_at is None:
                    row.submitted_at = now
                if to_status == "filled" and row.filled_at is None:
                    row.filled_at = now
                if exchange_order_id is not None:
                    row.exchange_order_id = exchange_order_id
                if raw_response is not None:
                    row.raw_response_json = raw_response

                await session.commit()
            except OrderStaleStateError:
                raise
            except Exception:
                await session.rollback()
                raise

            await session.refresh(row)
            logger.debug(
                "order_repo: transitioned id={} {}->{} actor={} event={}",
                order_id,
                current,
                to_status,
                actor,
                event_type,
            )
            return _row_to_result(row)

    async def link_to_trade(self, order_id: int, trade_id: int) -> None:
        """Backpopulate ``trade_id`` once the matching trade exists.

        Called by FASE 9.4 helper ``link_orders_to_trade``. Does not
        write an event row — this is a structural FK that doesn't
        change the order's status.
        """
        async with self._sf() as session, session.begin():
            row = await session.get(OrderRow, order_id)
            if row is None:
                raise ValueError(f"order #{order_id} not found")
            row.trade_id = trade_id

    # ─── Reads ─────────────────────────────────────────────────────

    async def get(self, order_id: int) -> OrderResult | None:
        async with self._sf() as session:
            row = await session.get(OrderRow, order_id)
            return _row_to_result(row) if row is not None else None

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> OrderResult | None:
        async with self._sf() as session:
            stmt = select(OrderRow).where(
                OrderRow.client_order_id == client_order_id
            )
            row = (await session.scalars(stmt)).first()
            return _row_to_result(row) if row is not None else None

    async def list_by_signal(self, signal_id: int) -> list[OrderResult]:
        async with self._sf() as session:
            stmt = (
                select(OrderRow)
                .where(OrderRow.signal_id == signal_id)
                .order_by(OrderRow.created_at.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_row_to_result(r) for r in rows]

    async def list_open_by_status(
        self, statuses: tuple[OrderStatus, ...] = ("submitted", "partially_filled")
    ) -> list[OrderResult]:
        async with self._sf() as session:
            stmt = select(OrderRow).where(OrderRow.status.in_(statuses))
            rows = (await session.scalars(stmt)).all()
            return [_row_to_result(r) for r in rows]

    async def list_events(self, order_id: int) -> list[OrderStatusEvent]:
        async with self._sf() as session:
            stmt = (
                select(OrderStatusEvent)
                .where(OrderStatusEvent.order_id == order_id)
                .order_by(
                    OrderStatusEvent.created_at.asc(),
                    OrderStatusEvent.id.asc(),
                )
            )
            rows = (await session.scalars(stmt)).all()
            return list(rows)


# ─── Translation helpers ───────────────────────────────────────────

def _row_to_result(row: OrderRow) -> OrderResult:
    side: OrderSide = _coerce_side(row.side)
    type_: OrderType = _coerce_type(row.type)
    status: OrderStatus = _coerce_status(row.status)
    price = Decimal(str(row.price)) if row.price is not None else None
    amount = Decimal(str(row.amount))
    return OrderResult(
        order_id=row.id,
        client_order_id=row.client_order_id,
        exchange_order_id=row.exchange_order_id,
        status=status,
        side=side,
        type=type_,
        amount=amount,
        price=price,
        reason=None,
        raw_response_json=row.raw_response_json,
        decided_at=None,
    )


def _coerce_side(raw: str) -> OrderSide:
    if raw not in ("buy", "sell"):
        raise ValueError(f"unexpected order side in DB: {raw!r}")
    return raw  # type: ignore[return-value]


def _coerce_type(raw: str) -> OrderType:
    if raw not in ("limit", "market", "stop_market", "stop_limit"):
        raise ValueError(f"unexpected order type in DB: {raw!r}")
    return raw  # type: ignore[return-value]


def _coerce_status(raw: str) -> OrderStatus:
    if raw not in ORDER_STATUSES:
        raise ValueError(f"unexpected order status in DB: {raw!r}")
    return raw  # type: ignore[return-value]
