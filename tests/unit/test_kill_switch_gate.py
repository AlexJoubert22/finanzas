"""Tests for :class:`KillSwitchGate`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import PortfolioSnapshot
from mib.trading.risk.gates.kill_switch import KillSwitchGate
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


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[],
        positions=[],
        equity_quote=Decimal(0),
        last_synced_at=datetime.now(UTC),
        source="dry-run",
    )


async def _seed(*, enabled: bool = False, killed_until: datetime | None = None) -> None:
    until_str = "NULL" if killed_until is None else f"'{killed_until.isoformat()}'"
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO trading_state "
                    "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                    " killed_until, last_modified_at, last_modified_by) "
                    f"VALUES (1, {1 if enabled else 0}, 0.03, 0.25, "
                    f"{until_str}, CURRENT_TIMESTAMP, 'test')"
                )
            )


def _gate() -> KillSwitchGate:
    return KillSwitchGate(TradingStateService(async_session_factory))


@pytest.mark.asyncio
async def test_rejects_when_enabled_false(fresh_db: None) -> None:  # noqa: ARG001
    await _seed(enabled=False)
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is False
    assert result.gate_name == "kill_switch"
    assert "enabled is False" in result.reason


@pytest.mark.asyncio
async def test_rejects_when_killed_until_in_future(
    fresh_db: None,  # noqa: ARG001
) -> None:
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    await _seed(enabled=True, killed_until=future)
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is False
    assert "kill window active" in result.reason


@pytest.mark.asyncio
async def test_passes_when_killed_until_in_past(
    fresh_db: None,  # noqa: ARG001
) -> None:
    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    await _seed(enabled=True, killed_until=past)
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True
    assert "kill switch open" in result.reason


@pytest.mark.asyncio
async def test_passes_when_enabled_and_no_kill_window(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed(enabled=True, killed_until=None)
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True


def test_gate_name_is_class_attribute() -> None:
    """name must be accessible without instantiation (Gate Protocol contract)."""
    assert KillSwitchGate.name == "kill_switch"


@pytest.mark.asyncio
async def test_gate_only_reads_state(fresh_db: None) -> None:  # noqa: ARG001
    """KillSwitchGate must NOT mutate trading_state.

    Spies on TradingStateService.update and verifies it's never called.
    """
    await _seed(enabled=False)
    state = TradingStateService(async_session_factory)
    state.update = MagicMock()  # type: ignore[method-assign]
    gate = KillSwitchGate(state)
    await gate.check(_signal(), _portfolio(), get_settings())
    assert state.update.called is False
