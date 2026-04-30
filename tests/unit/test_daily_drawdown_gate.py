"""Tests for :class:`DailyDrawdownGate`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot
from mib.trading.risk.gates.daily_drawdown import DailyDrawdownGate
from mib.trading.risk.state import TradingStateService
from mib.trading.signals import Signal


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _portfolio(equity: Decimal = Decimal("1000")) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[
            Balance(asset="EUR", free=equity, used=Decimal(0), total=equity),
        ],
        positions=[],
        equity_quote=equity,
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


async def _seed(*, daily_dd_max_pct: float = 0.03) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO trading_state "
                    "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                    " killed_until, last_modified_at, last_modified_by) "
                    f"VALUES (1, 1, {daily_dd_max_pct}, 0.25, NULL, "
                    "CURRENT_TIMESTAMP, 'test')"
                )
            )


def _gate() -> DailyDrawdownGate:
    state = TradingStateService(async_session_factory)
    return DailyDrawdownGate(state, async_session_factory)


@pytest.mark.asyncio
async def test_passes_when_no_equity(fresh_db: None) -> None:  # noqa: ARG001
    """Equity = 0 → can't compute DD threshold; pass safely."""
    await _seed()
    pf = PortfolioSnapshot(
        balances=[],
        positions=[],
        equity_quote=Decimal(0),
        last_synced_at=datetime.now(UTC),
        source="dry-run",
    )
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is True
    assert "no equity" in result.reason


@pytest.mark.asyncio
async def test_passes_when_trades_table_missing(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """FASE 8.3 contract: trades table doesn't exist yet — robust path
    returns 0 PnL and lets the gate pass.
    """
    await _seed()
    # The fresh_db fixture creates only Base.metadata tables; `trades`
    # is not in the metadata until FASE 9, so the query below raises
    # OperationalError which DailyDrawdownGate must swallow.
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True
    assert "within threshold" in result.reason


@pytest.mark.asyncio
async def test_passes_when_today_pnl_within_threshold(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Realistic PnL > -3% × equity → passes."""
    await _seed()
    # Simulate the trades table existing with a small loss.
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS trades ("
                    " id INTEGER PRIMARY KEY, "
                    " realized_pnl_quote NUMERIC, "
                    " closed_at DATETIME"
                    ")"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO trades (realized_pnl_quote, closed_at) "
                    "VALUES (-10.0, CURRENT_TIMESTAMP)"
                )
            )
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    # 1000 EUR equity * 0.03 = 30 threshold; today_pnl=-10 > -30.
    assert result.passed is True


@pytest.mark.asyncio
async def test_kills_until_midnight_when_dd_breached(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """today_pnl below -3% × equity → kill window flips, signal rejected."""
    await _seed()
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS trades ("
                    " id INTEGER PRIMARY KEY, "
                    " realized_pnl_quote NUMERIC, "
                    " closed_at DATETIME"
                    ")"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO trades (realized_pnl_quote, closed_at) "
                    "VALUES (-100.0, CURRENT_TIMESTAMP)"
                )
            )
    gate = _gate()
    result = await gate.check(_signal(), _portfolio(), get_settings())
    # 1000 * 0.03 = 30; today_pnl = -100 < -30 → reject.
    assert result.passed is False
    assert "daily DD breached" in result.reason
    # killed_until was set on the singleton.
    state = await TradingStateService(async_session_factory).get()
    assert state.killed_until is not None
    assert state.last_modified_by == "gate:daily_drawdown"


@pytest.mark.asyncio
async def test_rejects_when_kill_window_already_active(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """If killed_until is already in the future, gate rejects without
    re-querying trades.
    """
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO trading_state "
                    "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                    " killed_until, last_modified_at, last_modified_by) "
                    f"VALUES (1, 1, 0.03, 0.25, '{future.isoformat()}', "
                    "CURRENT_TIMESTAMP, 'test')"
                )
            )
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is False
    assert "DD kill window already active" in result.reason


def test_gate_name_is_class_attribute() -> None:
    assert DailyDrawdownGate.name == "daily_drawdown"
