"""Tests for :class:`MinAIConfidenceGate` (FASE 11.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot
from mib.trading.risk.gates.min_ai_confidence import (
    DEFAULT_MIN_AI_CONFIDENCE,
    MinAIConfidenceGate,
)
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal(confidence_ai: float | None = None) -> Signal:
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
        confidence_ai=confidence_ai,
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[
            Balance(asset="EUR", free=Decimal("1000"), used=Decimal(0), total=Decimal("1000")),
        ],
        positions=[],
        equity_quote=Decimal("1000"),
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


# ─── Threshold validation ───────────────────────────────────────────


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold must be in"):
        MinAIConfidenceGate(threshold=1.5)
    with pytest.raises(ValueError, match="threshold must be in"):
        MinAIConfidenceGate(threshold=-0.1)


def test_default_threshold_is_055() -> None:
    """Spec lock-in: default is 0.55."""
    assert DEFAULT_MIN_AI_CONFIDENCE == 0.55
    g = MinAIConfidenceGate()
    assert g.name == "min_ai_confidence"


# ─── check() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passes_when_confidence_above_threshold() -> None:
    g = MinAIConfidenceGate(threshold=0.55)
    result = await g.check(
        _signal(confidence_ai=0.8), _portfolio(), get_settings()
    )
    assert result.passed is True
    assert result.gate_name == "min_ai_confidence"


@pytest.mark.asyncio
async def test_passes_at_threshold() -> None:
    """Boundary: confidence == threshold → pass."""
    g = MinAIConfidenceGate(threshold=0.55)
    result = await g.check(
        _signal(confidence_ai=0.55), _portfolio(), get_settings()
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_rejects_when_confidence_below_threshold() -> None:
    g = MinAIConfidenceGate(threshold=0.55)
    result = await g.check(
        _signal(confidence_ai=0.4), _portfolio(), get_settings()
    )
    assert result.passed is False
    assert "0.40" in result.reason
    assert "0.55" in result.reason


@pytest.mark.asyncio
async def test_passes_when_confidence_is_none_validator_pending() -> None:
    """Missing confidence_ai → gate passes (validator is canonical)."""
    g = MinAIConfidenceGate(threshold=0.55)
    result = await g.check(
        _signal(confidence_ai=None), _portfolio(), get_settings()
    )
    assert result.passed is True
    assert "no confidence_ai" in result.reason


# ─── set_ai_confidence on signal_repo ───────────────────────────────


@pytest.mark.asyncio
async def test_set_ai_confidence_persists(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = SignalRepository(async_session_factory)
    persisted = await repo.add(_signal(confidence_ai=None))
    ok = await repo.set_ai_confidence(persisted.id, 0.83)
    assert ok is True
    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.signal.confidence_ai == 0.83


@pytest.mark.asyncio
async def test_set_ai_confidence_unknown_returns_false(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = SignalRepository(async_session_factory)
    assert await repo.set_ai_confidence(9999, 0.7) is False


@pytest.mark.asyncio
async def test_set_ai_confidence_validates_range(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = SignalRepository(async_session_factory)
    persisted = await repo.add(_signal(confidence_ai=None))
    with pytest.raises(ValueError, match="confidence_ai must be"):
        await repo.set_ai_confidence(persisted.id, 1.5)
    with pytest.raises(ValueError, match="confidence_ai must be"):
        await repo.set_ai_confidence(persisted.id, -0.1)


# ─── Default RiskManager wiring (off by default) ────────────────────


@pytest.mark.asyncio
async def test_settings_default_does_not_register_gate(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Default settings: risk_use_ai_confidence=False → not in chain."""
    s = get_settings()
    assert s.risk_use_ai_confidence is False
