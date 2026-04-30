"""Tests for :class:`OrderRepository` (FASE 9.2 append-only contract)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from mib.db.session import async_session_factory
from mib.trading.order_repo import (
    OrderRepository,
    OrderStaleStateError,
    derive_client_order_id,
)
from mib.trading.orders import OrderInputs
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal_inputs(signal_id: int = 1, *, amount: str = "0.001", price: str = "60000") -> OrderInputs:
    return OrderInputs(
        signal_id=signal_id,
        symbol="BTC/USDT",
        side="buy",
        type="limit",
        amount=Decimal(amount),
        price=Decimal(price),
    )


def _signal() -> Signal:
    from datetime import UTC, datetime  # noqa: PLC0415

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
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


async def _seed_signal() -> int:
    sr = SignalRepository(async_session_factory)
    p = await sr.add(_signal())
    return p.id


@pytest.fixture
def repo() -> OrderRepository:
    return OrderRepository(async_session_factory)


# ─── derive_client_order_id ──────────────────────────────────────────

def test_client_order_id_deterministic() -> None:
    a = derive_client_order_id(_signal_inputs())
    b = derive_client_order_id(_signal_inputs())
    assert a == b
    assert a.startswith("mib-1-")


def test_client_order_id_differs_on_param_change() -> None:
    base = derive_client_order_id(_signal_inputs())
    changed_amount = derive_client_order_id(_signal_inputs(amount="0.002"))
    changed_price = derive_client_order_id(_signal_inputs(price="60001"))
    assert base != changed_amount
    assert base != changed_price


# ─── add_or_get ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_or_get_inserts_first_call(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    inputs = _signal_inputs(sid)
    result = await repo.add_or_get(
        inputs, exchange_id="binance_sandbox", raw_payload={"foo": "bar"}
    )
    assert result.order_id > 0
    assert result.status == "created"
    assert result.client_order_id.startswith(f"mib-{sid}-")
    assert result.exchange_order_id is None


@pytest.mark.asyncio
async def test_add_or_get_idempotent_on_duplicate(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Same inputs twice → same row, no duplicate."""
    sid = await _seed_signal()
    inputs = _signal_inputs(sid)
    first = await repo.add_or_get(
        inputs, exchange_id="binance_sandbox", raw_payload={}
    )
    second = await repo.add_or_get(
        inputs, exchange_id="binance_sandbox", raw_payload={}
    )
    assert first.order_id == second.order_id
    assert first.client_order_id == second.client_order_id


@pytest.mark.asyncio
async def test_add_writes_created_event(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    result = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    events = await repo.list_events(result.order_id)
    assert len(events) == 1
    assert events[0].event_type == "created"
    assert events[0].from_status is None
    assert events[0].to_status == "created"
    assert events[0].actor == "system"


# ─── transition ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_writes_event_and_updates_cache(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    created = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    updated = await repo.transition(
        created.order_id,
        "submitted",
        actor="ccxt-trader:exchange",
        event_type="submitted",
        exchange_order_id="exch-12345",
        raw_response={"id": "exch-12345"},
    )
    assert updated is not None
    assert updated.status == "submitted"
    assert updated.exchange_order_id == "exch-12345"
    events = await repo.list_events(created.order_id)
    assert [e.event_type for e in events] == ["created", "submitted"]


@pytest.mark.asyncio
async def test_transition_preserves_audit_chain(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """created → submitted → filled writes 3 events; cache holds last."""
    sid = await _seed_signal()
    o = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    await repo.transition(
        o.order_id, "submitted", actor="exchange", event_type="submitted"
    )
    await repo.transition(
        o.order_id, "filled", actor="exchange", event_type="filled",
        raw_response={"filled": "0.001"},
    )
    final = await repo.get(o.order_id)
    assert final is not None
    assert final.status == "filled"
    events = await repo.list_events(o.order_id)
    assert [e.to_status for e in events] == ["created", "submitted", "filled"]


@pytest.mark.asyncio
async def test_transition_rejects_invalid_status(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    o = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    with pytest.raises(ValueError, match="invalid OrderStatus"):
        await repo.transition(
            o.order_id, "magical_state", actor="x", event_type="filled"  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_transition_stale_from_status_raises(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    o = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    await repo.transition(
        o.order_id, "submitted", actor="exchange", event_type="submitted"
    )
    with pytest.raises(OrderStaleStateError):
        await repo.transition(
            o.order_id,
            "filled",
            actor="exchange",
            event_type="filled",
            expected_from_status="created",  # but it's already "submitted"
        )


@pytest.mark.asyncio
async def test_transition_unknown_id_returns_none(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    out = await repo.transition(
        9999, "submitted", actor="x", event_type="submitted"
    )
    assert out is None


@pytest.mark.asyncio
async def test_link_to_trade_backpopulates(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """``link_to_trade`` sets trade_id without writing an event row.

    The trades table doesn't exist until 9.4, so we use a
    placeholder int — the FK is added in 9.4's migration.
    """
    sid = await _seed_signal()
    o = await repo.add_or_get(
        _signal_inputs(sid), exchange_id="binance_sandbox", raw_payload={}
    )
    await repo.link_to_trade(o.order_id, trade_id=42)
    # Verify by reading the raw row through a fresh query.
    from mib.db.models import OrderRow  # noqa: PLC0415

    async with async_session_factory() as session:
        row = await session.get(OrderRow, o.order_id)
        assert row is not None
        assert row.trade_id == 42
    # No event row was written.
    events = await repo.list_events(o.order_id)
    assert len(events) == 1  # only the original 'created'


@pytest.mark.asyncio
async def test_list_by_signal_orders_chronologically(
    repo: OrderRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    a = await repo.add_or_get(
        _signal_inputs(sid, amount="0.001"),
        exchange_id="binance_sandbox", raw_payload={},
    )
    b = await repo.add_or_get(
        _signal_inputs(sid, amount="0.002"),
        exchange_id="binance_sandbox", raw_payload={},
    )
    listed = await repo.list_by_signal(sid)
    assert [o.order_id for o in listed] == [a.order_id, b.order_id]
