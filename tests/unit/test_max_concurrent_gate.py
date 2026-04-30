"""Tests for :class:`MaxConcurrentTradesGate`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot, Position
from mib.trading.risk.gates.max_concurrent import MaxConcurrentTradesGate
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal(ticker: str = "BTC/USDT") -> Signal:
    return Signal(
        ticker=ticker,
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_010.0),
        invalidation=58_800.0,
        target_1=61_200.0,
        target_2=63_600.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _portfolio(positions: list[Position]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[Balance(asset="EUR", free=Decimal(1000), used=Decimal(0), total=Decimal(1000))],
        positions=positions,
        equity_quote=Decimal(1000),
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


def _position(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        side="long",
        amount=Decimal("0.01"),
        entry_price=Decimal(50_000),
        mark_price=Decimal(50_000),
        unrealized_pnl=Decimal(0),
        leverage=1.0,
    )


def _gate() -> MaxConcurrentTradesGate:
    return MaxConcurrentTradesGate(SignalRepository(async_session_factory))


@pytest.mark.asyncio
async def test_passes_when_under_cap(fresh_db: None) -> None:  # noqa: ARG001
    pf = _portfolio([_position("BTC/USDT"), _position("ETH/USDT")])
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is True


@pytest.mark.asyncio
async def test_rejects_when_at_cap_via_realized(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """5 open positions == default cap → reject."""
    pf = _portfolio(
        [
            _position("BTC/USDT"),
            _position("ETH/USDT"),
            _position("SOL/USDT"),
            _position("AVAX/USDT"),
            _position("AAPL"),
        ]
    )
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is False
    assert ">= cap" in result.reason


@pytest.mark.asyncio
async def test_consumed_signals_count_as_proxy_positions(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """In FASE 8 reality (no trades table), consumed signals are
    counted as if they were open positions.
    """
    repo = SignalRepository(async_session_factory)
    # Create 5 consumed signals → at cap.
    for ticker in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AAPL", "MSFT"]:
        ps = await repo.add(_signal(ticker))
        await repo.transition(
            ps.id, "consumed", actor="user:test", event_type="approved"
        )
    pf = _portfolio(positions=[])  # nothing realized
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is False
    assert "consumed_pending=5" in result.reason


@pytest.mark.asyncio
async def test_pending_signals_not_counted(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Signals in 'pending' (awaiting operator approval) are NOT proxy
    positions. Only consumed counts.
    """
    repo = SignalRepository(async_session_factory)
    for ticker in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "AAPL", "MSFT"]:
        await repo.add(_signal(ticker))
    pf = _portfolio(positions=[])
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is True


def test_gate_name_is_class_attribute() -> None:
    assert MaxConcurrentTradesGate.name == "max_concurrent_trades"
