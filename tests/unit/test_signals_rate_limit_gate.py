"""Tests for :class:`SignalsPerHourRateLimitGate`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot
from mib.trading.risk.gates.signals_rate_limit import SignalsPerHourRateLimitGate
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
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


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[Balance(asset="EUR", free=Decimal(1000), used=Decimal(0), total=Decimal(1000))],
        positions=[],
        equity_quote=Decimal(1000),
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


async def _seed_approved_event(*, minutes_ago: int) -> None:
    """Insert an 'approved' event in signal_status_events at a given offset.

    Uses SQLAlchemy parameter binding so the datetime is serialised in
    the same format the ORM uses (space separator, microseconds).
    Mismatched ``T``-separator strings would sort lexicographically
    above the cutoff and break the rolling window comparison.
    """
    s_repo = SignalRepository(async_session_factory)
    ps = await s_repo.add(_signal())
    when = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO signal_status_events "
                    "(signal_id, from_status, to_status, event_type, "
                    " actor, reason, metadata_json, created_at) "
                    "VALUES "
                    "(:sid, 'pending', 'consumed', 'approved', "
                    " 'user:test', NULL, NULL, :ts)"
                ),
                {"sid": ps.id, "ts": when},
            )


def _gate() -> SignalsPerHourRateLimitGate:
    return SignalsPerHourRateLimitGate(async_session_factory)


@pytest.mark.asyncio
async def test_passes_with_zero_recent_approvals(
    fresh_db: None,  # noqa: ARG001
) -> None:
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True
    assert "0 < cap" in result.reason


@pytest.mark.asyncio
async def test_rejects_when_at_cap(fresh_db: None) -> None:  # noqa: ARG001
    """Default cap is 2; insert 2 approved events in last 30min."""
    await _seed_approved_event(minutes_ago=10)
    await _seed_approved_event(minutes_ago=30)
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is False
    assert "= 2 >= cap" in result.reason


@pytest.mark.asyncio
async def test_only_counts_approved_event_type(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Other event types (created, expired, cancelled) do NOT count."""
    s_repo = SignalRepository(async_session_factory)
    # 'created' events — already exist from the add() in the helper.
    for _ in range(5):
        await s_repo.add(_signal())
    # Default cap 2; 5 'created' but 0 'approved' → passes.
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True


@pytest.mark.asyncio
async def test_rolling_window_excludes_old_events(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """An approved event >60min ago must not count."""
    await _seed_approved_event(minutes_ago=70)  # outside window
    await _seed_approved_event(minutes_ago=30)  # inside
    result = await _gate().check(_signal(), _portfolio(), get_settings())
    assert result.passed is True  # only 1 inside window, cap 2


def test_gate_name_is_class_attribute() -> None:
    assert SignalsPerHourRateLimitGate.name == "signals_per_hour"
