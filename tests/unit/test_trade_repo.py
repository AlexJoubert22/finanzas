"""Tests for :class:`TradeRepository` (FASE 9.4 append-only contract)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.db.models import OrderRow, TradeRow
from mib.db.session import async_session_factory
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderInputs
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.trade_repo import (
    TradeAlreadyExistsError,
    TradeRepository,
    TradeStaleStateError,
)
from mib.trading.trades import TradeInputs


def _signal(strategy: str = "scanner.oversold.v1") -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=61_200.0,
        target_2=63_600.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id=strategy,
        confidence_ai=None,
    )


async def _seed_signal(strategy: str = "scanner.oversold.v1") -> int:
    sr = SignalRepository(async_session_factory)
    p = await sr.add(_signal(strategy=strategy))
    return p.id


def _trade_inputs(signal_id: int) -> TradeInputs:
    return TradeInputs(
        signal_id=signal_id,
        ticker="BTC/USDT",
        side="long",
        size=Decimal("0.001"),
        entry_price=Decimal("60000"),
        stop_loss_price=Decimal("58800"),
        take_profit_price=Decimal("63600"),
        exchange_id="binance_sandbox",
        metadata={"strategy_id": "scanner.oversold.v1"},
    )


@pytest.fixture
def repo() -> TradeRepository:
    return TradeRepository(async_session_factory)


@pytest.fixture
def order_repo() -> OrderRepository:
    return OrderRepository(async_session_factory)


# ─── add() ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_creates_pending_trade_with_event(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    assert trade.trade_id > 0
    assert trade.signal_id == sid
    assert trade.status == "pending"
    assert trade.size == Decimal("0.001")
    assert trade.entry_price == Decimal("60000")
    assert trade.stop_loss_price == Decimal("58800")
    assert trade.take_profit_price == Decimal("63600")
    assert trade.exit_price is None
    assert trade.closed_at is None
    assert trade.realized_pnl_quote is None
    assert trade.fees_paid_quote == Decimal(0)
    assert trade.exchange_id == "binance_sandbox"
    assert trade.metadata_json == {"strategy_id": "scanner.oversold.v1"}

    events = await repo.list_events(trade.trade_id)
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].from_status is None
    assert events[0].to_status == "pending"
    assert events[0].actor == "system"


@pytest.mark.asyncio
async def test_add_rejects_duplicate_signal(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """One trade per signal — UNIQUE(signal_id) blocks duplicates."""
    sid = await _seed_signal()
    await repo.add(_trade_inputs(sid))
    with pytest.raises(TradeAlreadyExistsError) as exc_info:
        await repo.add(_trade_inputs(sid))
    assert exc_info.value.signal_id == sid


# ─── transition() ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_pending_to_open(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    updated = await repo.transition(
        trade.trade_id,
        "open",
        actor="executor",
        event_type="opened",
        expected_from_status="pending",
    )
    assert updated is not None
    assert updated.status == "open"
    assert updated.closed_at is None
    events = await repo.list_events(trade.trade_id)
    assert [e.to_status for e in events] == ["pending", "open"]
    assert events[1].event_type == "opened"
    assert events[1].from_status == "pending"


@pytest.mark.asyncio
async def test_transition_open_to_closed_with_pnl(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    await repo.transition(
        trade.trade_id, "open", actor="executor", event_type="opened",
        expected_from_status="pending",
    )
    closed = await repo.transition(
        trade.trade_id,
        "closed",
        actor="reconciler",
        event_type="closed",
        expected_from_status="open",
        exit_price=Decimal("61500"),
        realized_pnl_quote=Decimal("1.5"),
        fees_increment=Decimal("0.05"),
    )
    assert closed is not None
    assert closed.status == "closed"
    assert closed.exit_price == Decimal("61500")
    assert closed.realized_pnl_quote == Decimal("1.5")
    assert closed.fees_paid_quote == Decimal("0.05")
    assert closed.closed_at is not None
    events = await repo.list_events(trade.trade_id)
    assert [e.to_status for e in events] == ["pending", "open", "closed"]


@pytest.mark.asyncio
async def test_transition_failed_sets_closed_at(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    failed = await repo.transition(
        trade.trade_id,
        "failed",
        actor="executor",
        event_type="failed",
        reason="entry rejected",
        expected_from_status="pending",
    )
    assert failed is not None
    assert failed.status == "failed"
    assert failed.closed_at is not None


@pytest.mark.asyncio
async def test_transition_stale_from_status_raises(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    await repo.transition(
        trade.trade_id, "open", actor="executor", event_type="opened",
        expected_from_status="pending",
    )
    with pytest.raises(TradeStaleStateError) as exc_info:
        await repo.transition(
            trade.trade_id,
            "closed",
            actor="reconciler",
            event_type="closed",
            expected_from_status="pending",  # but it's already "open"
        )
    assert exc_info.value.trade_id == trade.trade_id
    assert exc_info.value.expected == "pending"
    assert exc_info.value.actual == "open"


@pytest.mark.asyncio
async def test_transition_rejects_invalid_status(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    with pytest.raises(ValueError, match="invalid TradeStatus"):
        await repo.transition(
            trade.trade_id,
            "magical",  # type: ignore[arg-type]
            actor="x",
            event_type="opened",
        )


@pytest.mark.asyncio
async def test_transition_unknown_id_returns_none(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    out = await repo.transition(
        9999, "open", actor="x", event_type="opened"
    )
    assert out is None


@pytest.mark.asyncio
async def test_transition_concurrent_one_wins(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Two concurrent transitions on the same trade: only one succeeds.

    BEGIN IMMEDIATE serialises writers; the loser sees the new status
    and gets a ``TradeStaleStateError`` because both used the same
    ``expected_from_status``.
    """
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))

    async def _try() -> str:
        try:
            await repo.transition(
                trade.trade_id,
                "open",
                actor="executor",
                event_type="opened",
                expected_from_status="pending",
            )
            return "ok"
        except TradeStaleStateError:
            return "stale"

    results = await asyncio.gather(_try(), _try())
    assert sorted(results) == ["ok", "stale"]
    final = await repo.get(trade.trade_id)
    assert final is not None
    assert final.status == "open"
    events = await repo.list_events(trade.trade_id)
    # exactly one 'opened' event survived
    assert sum(1 for e in events if e.event_type == "opened") == 1


# ─── link_orders_to_trade() ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_link_orders_to_trade_backpopulates(
    repo: TradeRepository,
    order_repo: OrderRepository,
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    o_entry = await order_repo.add_or_get(
        OrderInputs(
            signal_id=sid,
            symbol="BTC/USDT",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )
    o_stop = await order_repo.add_or_get(
        OrderInputs(
            signal_id=sid,
            symbol="BTC/USDT",
            side="sell",
            type="stop_market",
            amount=Decimal("0.001"),
            price=Decimal("58800"),
            reduce_only=True,
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )

    await repo.link_orders_to_trade(
        trade.trade_id, [o_entry.order_id, o_stop.order_id]
    )

    async with async_session_factory() as session:
        for oid in (o_entry.order_id, o_stop.order_id):
            row = await session.get(OrderRow, oid)
            assert row is not None
            assert row.trade_id == trade.trade_id

    # No status events written for the link operation.
    entry_events = await order_repo.list_events(o_entry.order_id)
    assert [e.event_type for e in entry_events] == ["created"]


@pytest.mark.asyncio
async def test_link_orders_to_trade_empty_list_noop(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    # Should not raise.
    await repo.link_orders_to_trade(trade.trade_id, [])


@pytest.mark.asyncio
async def test_link_orders_to_trade_unknown_order_raises(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    with pytest.raises(ValueError, match="order #9999 not found"):
        await repo.link_orders_to_trade(trade.trade_id, [9999])


# ─── reads ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_by_signal(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    created = await repo.add(_trade_inputs(sid))
    fetched = await repo.get_by_signal(sid)
    assert fetched is not None
    assert fetched.trade_id == created.trade_id


@pytest.mark.asyncio
async def test_get_by_signal_unknown_returns_none(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    out = await repo.get_by_signal(9999)
    assert out is None


@pytest.mark.asyncio
async def test_list_open_includes_pending_and_open(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid_a = await _seed_signal(strategy="scanner.oversold.v1")
    sid_b = await _seed_signal(strategy="scanner.breakout.v1")
    sid_c = await _seed_signal(strategy="scanner.momentum.v1")
    t_pending = await repo.add(_trade_inputs(sid_a))
    t_open = await repo.add(_trade_inputs(sid_b))
    t_closed = await repo.add(_trade_inputs(sid_c))
    await repo.transition(
        t_open.trade_id, "open", actor="x", event_type="opened",
        expected_from_status="pending",
    )
    await repo.transition(
        t_closed.trade_id, "open", actor="x", event_type="opened",
        expected_from_status="pending",
    )
    await repo.transition(
        t_closed.trade_id, "closed", actor="x", event_type="closed",
        expected_from_status="open",
        exit_price=Decimal("61000"),
        realized_pnl_quote=Decimal("1.0"),
    )

    open_trades = await repo.list_open()
    open_ids = {t.trade_id for t in open_trades}
    assert t_pending.trade_id in open_ids
    assert t_open.trade_id in open_ids
    assert t_closed.trade_id not in open_ids


# ─── Append-only invariant ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_events_are_append_only(
    repo: TradeRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Every transition adds a new row; nothing rewrites prior events."""
    sid = await _seed_signal()
    trade = await repo.add(_trade_inputs(sid))
    await repo.transition(
        trade.trade_id, "open", actor="x", event_type="opened",
        expected_from_status="pending",
    )
    await repo.transition(
        trade.trade_id, "closed", actor="x", event_type="closed",
        expected_from_status="open",
        exit_price=Decimal("61000"),
        realized_pnl_quote=Decimal("1.0"),
    )
    events = await repo.list_events(trade.trade_id)
    assert len(events) == 3

    # Verify the trade row count never duplicated either.
    async with async_session_factory() as session:
        from sqlalchemy import select  # noqa: PLC0415

        count = (await session.scalars(
            select(TradeRow).where(TradeRow.signal_id == sid)
        )).all()
        assert len(count) == 1
