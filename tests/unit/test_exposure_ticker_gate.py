"""Tests for :class:`ExposurePerTickerGate`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot, Position
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.gates.exposure_ticker import ExposurePerTickerGate
from mib.trading.risk.protocol import GateResult
from mib.trading.risk.repo import RiskDecisionRepository
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


def _portfolio_with_position(
    ticker: str,
    *,
    amount: Decimal,
    mark_price: Decimal,
    equity: Decimal = Decimal("10000"),
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[
            Balance(asset="EUR", free=equity, used=Decimal(0), total=equity),
        ],
        positions=[
            Position(
                symbol=ticker,
                side="long",
                amount=amount,
                entry_price=mark_price,
                mark_price=mark_price,
                unrealized_pnl=Decimal(0),
                leverage=1.0,
            ),
        ],
        equity_quote=equity,
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


def _empty_portfolio(equity: Decimal = Decimal("10000")) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[
            Balance(asset="EUR", free=equity, used=Decimal(0), total=equity),
        ],
        positions=[],
        equity_quote=equity,
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


def _gate() -> ExposurePerTickerGate:
    return ExposurePerTickerGate(
        SignalRepository(async_session_factory),
        RiskDecisionRepository(async_session_factory),
    )


@pytest.mark.asyncio
async def test_passes_when_zero_equity(fresh_db: None) -> None:  # noqa: ARG001
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
async def test_passes_with_no_existing_exposure(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Empty portfolio + no consumed signals → headroom = full cap."""
    result = await _gate().check(_signal(), _empty_portfolio(), get_settings())
    assert result.passed is True
    assert "headroom" in result.reason
    assert isinstance(result, GateResult)


@pytest.mark.asyncio
async def test_passes_when_existing_position_under_cap(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """1 BTC at 50k = 50k notional, 10k equity * 0.15 cap = 1.5k. Above cap.
    Use a smaller position: 0.01 BTC at 50k = 500 notional, well under
    1.5k cap.
    """
    pf = _portfolio_with_position(
        "BTC/USDT", amount=Decimal("0.01"), mark_price=Decimal("50000"),
    )
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is True


@pytest.mark.asyncio
async def test_rejects_when_existing_position_over_cap(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """0.05 BTC at 50k = 2500 notional, equity 10k * 0.15 = 1500 cap → reject."""
    pf = _portfolio_with_position(
        "BTC/USDT", amount=Decimal("0.05"), mark_price=Decimal("50000"),
    )
    result = await _gate().check(_signal(), pf, get_settings())
    assert result.passed is False
    assert "exposure" in result.reason
    assert ">= cap" in result.reason


@pytest.mark.asyncio
async def test_only_counts_matching_ticker(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Position on ETH/USDT shouldn't affect BTC/USDT exposure."""
    pf = _portfolio_with_position(
        "ETH/USDT", amount=Decimal("100"), mark_price=Decimal("3000"),
    )
    result = await _gate().check(_signal("BTC/USDT"), pf, get_settings())
    assert result.passed is True


@pytest.mark.asyncio
async def test_includes_sized_pending_decisions(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """A signal in 'consumed' status with a sized RiskDecision counts
    against the per-ticker cap, even before FASE 9 actually opens
    the trade.
    """
    s_repo = SignalRepository(async_session_factory)
    d_repo = RiskDecisionRepository(async_session_factory)

    persisted = await s_repo.add(_signal("BTC/USDT"))
    await s_repo.transition(
        persisted.id, "consumed", actor="user:test", event_type="approved"
    )
    # Big sized amount that alone breaches the 1500 cap.
    big_decision = RiskDecision(
        signal_id=persisted.id,
        version=1,
        approved=True,
        gate_results=(),
        reasoning="test",
        decided_at=datetime.now(UTC),
        sized_amount=Decimal("2000"),
    )
    await d_repo.add(big_decision)

    # Empty position state, but the sized pending alone breaches.
    result = await _gate().check(
        _signal("BTC/USDT"), _empty_portfolio(), get_settings()
    )
    assert result.passed is False
    assert "pending_sized" in result.reason


@pytest.mark.asyncio
async def test_skips_decisions_without_sized_amount(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Pre-FASE-8.5 state: decision has sized_amount=None and must NOT
    contribute to the pending sum.
    """
    s_repo = SignalRepository(async_session_factory)
    d_repo = RiskDecisionRepository(async_session_factory)

    persisted = await s_repo.add(_signal("BTC/USDT"))
    await s_repo.transition(
        persisted.id, "consumed", actor="user:test", event_type="approved"
    )
    decision_no_size = RiskDecision(
        signal_id=persisted.id,
        version=1,
        approved=True,
        gate_results=(),
        reasoning="approved without size yet",
        decided_at=datetime.now(UTC),
        sized_amount=None,
    )
    await d_repo.add(decision_no_size)

    result = await _gate().check(
        _signal("BTC/USDT"), _empty_portfolio(), get_settings()
    )
    assert result.passed is True


def test_gate_name_is_class_attribute() -> None:
    assert ExposurePerTickerGate.name == "exposure_per_ticker"
