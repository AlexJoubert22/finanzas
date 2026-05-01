"""Tests for ``execute_panic`` (FASE 13.6)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from mib.db.session import async_session_factory
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderInputs, OrderResult
from mib.trading.panic import (
    PANIC_KILL_WINDOW_DAYS,
    PanicReport,
    execute_panic,
)
from mib.trading.risk.state import TradingStateService
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import TradeInputs


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=63_000.0,
        target_2=66_000.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


async def _seed_open_trade(*, ticker: str = "BTC/USDT") -> int:
    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    persisted = await sig_repo.add(_signal())
    trade = await trade_repo.add(
        TradeInputs(
            signal_id=persisted.id,
            ticker=ticker,
            side="long",
            size=Decimal("0.001"),
            entry_price=Decimal("60000"),
            stop_loss_price=Decimal("58800"),
            exchange_id="binance_sandbox",
        )
    )
    await trade_repo.transition(
        trade.trade_id, "open",
        actor="seed", event_type="opened",
        expected_from_status="pending",
    )
    return trade.trade_id


async def _seed_open_order() -> int:
    sig_repo = SignalRepository(async_session_factory)
    persisted = await sig_repo.add(_signal())
    repo = OrderRepository(async_session_factory)
    inputs = OrderInputs(
        signal_id=persisted.id,
        symbol="BTC/USDT",
        side="buy",
        type="limit",
        amount=Decimal("0.001"),
        price=Decimal("60000"),
    )
    res = await repo.add_or_get(
        inputs, exchange_id="binance_sandbox",
        raw_payload={"symbol": "BTC/USDT"},
    )
    await repo.transition(
        res.order_id,
        "submitted",
        actor="seed", event_type="submitted",
        exchange_order_id="exch-123",
    )
    return res.order_id


async def _seed_state() -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                "VALUES (1, 1, 0.03, 0.25, NULL, 'paper', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


# ─── Stub trader ─────────────────────────────────────────────────────


class _StubTrader:
    """Records cancel/close calls; returns canned dicts."""

    def __init__(self, *, raise_on_cancel: bool = False) -> None:
        self.cancels: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []
        self._raise_on_cancel = raise_on_cancel

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if self._raise_on_cancel:
            raise RuntimeError("cancel boom")
        record = {
            "symbol": symbol,
            "exchange_order_id": exchange_order_id,
            "client_order_id": client_order_id,
        }
        self.cancels.append(record)
        return {"id": exchange_order_id or "stub-cancel"}

    async def close_position(
        self, symbol: str, side: str, amount: float
    ) -> dict[str, Any]:
        self.closes.append({"symbol": symbol, "side": side, "amount": amount})
        return {"id": "stub-close"}


# ─── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panic_cancels_orders_closes_trades_kills(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_order()
    await _seed_open_trade()

    trader = _StubTrader()
    report = await execute_panic(
        actor="user:1",
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
        state_service=TradingStateService(async_session_factory),
    )
    assert report.cancelled_count == 1
    assert report.closed_count == 1
    assert trader.cancels[0]["exchange_order_id"] == "exch-123"
    assert trader.closes[0]["side"] == "sell"  # opposite of long
    assert report.errors == []

    # Kill switch flipped + 7-day window applied.
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False
    assert state.killed_until is not None
    expected_at_least = (
        datetime.now(UTC).replace(tzinfo=None)
        + timedelta(days=PANIC_KILL_WINDOW_DAYS - 1)
    )
    assert state.killed_until >= expected_at_least - timedelta(hours=1)
    assert state.last_modified_by.startswith("panic:")


@pytest.mark.asyncio
async def test_panic_continues_on_per_order_cancel_failure(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_order()
    await _seed_open_trade()

    trader = _StubTrader(raise_on_cancel=True)
    report = await execute_panic(
        actor="user:1",
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
        state_service=TradingStateService(async_session_factory),
    )
    # Cancel raised but close + kill still ran.
    assert report.cancelled_count == 0
    assert report.closed_count == 1
    assert any("cancel" in e for e in report.errors)
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False  # kill switch flipped despite errors


@pytest.mark.asyncio
async def test_panic_with_no_open_state_still_kills(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Empty system: no orders, no trades → kill switch still flips."""
    await _seed_state()
    trader = _StubTrader()
    report = await execute_panic(
        actor="user:1",
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
        state_service=TradingStateService(async_session_factory),
    )
    assert report.cancelled_count == 0
    assert report.closed_count == 0
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False


@pytest.mark.asyncio
async def test_panic_closes_short_with_buy_side(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Short trade closed with side=buy."""
    await _seed_state()
    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    short_signal = Signal(
        ticker="ETH/USDT",
        side="short",
        strength=0.7,
        timeframe="1h",
        entry_zone=(3000.0, 3000.0),
        invalidation=3060.0,
        target_1=2900.0,
        target_2=2800.0,
        rationale="t",
        indicators={"rsi_14": 75.0, "atr_14": 30.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.breakout.v1",
        confidence_ai=None,
    )
    persisted = await sig_repo.add(short_signal)
    trade = await trade_repo.add(
        TradeInputs(
            signal_id=persisted.id,
            ticker="ETH/USDT",
            side="short",
            size=Decimal("0.5"),
            entry_price=Decimal("3000"),
            stop_loss_price=Decimal("3060"),
            exchange_id="binance_sandbox",
        )
    )
    await trade_repo.transition(
        trade.trade_id, "open",
        actor="seed", event_type="opened",
        expected_from_status="pending",
    )

    trader = _StubTrader()
    report = await execute_panic(
        actor="user:1",
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
        state_service=TradingStateService(async_session_factory),
    )
    assert report.closed_count == 1
    assert trader.closes[0]["side"] == "buy"


@pytest.mark.asyncio
async def test_panic_meets_3s_latency_budget(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Spec target: <3s end-to-end with stub trader (no exchange calls)."""
    await _seed_state()
    # Seed a few orders + trades to make it less trivial.
    for _ in range(3):
        await _seed_open_order()
        await _seed_open_trade()

    trader = _StubTrader()
    t0 = time.monotonic()
    report = await execute_panic(
        actor="user:1",
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
        state_service=TradingStateService(async_session_factory),
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"panic took {elapsed:.2f}s, budget 3s"
    assert report.elapsed_seconds < 3.0


# Suppress unused-import warning for OrderResult.
_ = OrderResult
_ = PanicReport
