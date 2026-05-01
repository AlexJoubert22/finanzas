"""Tests for hardcoded temporal guards (FASE 10.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mib.db.session import async_session_factory
from mib.trading.mode import TradingMode
from mib.trading.mode_guards import (
    check_transition_allowed,
    closed_trades_in_mode,
    days_in_current_mode,
)
from mib.trading.mode_transitions_repo import ModeTransitionRepository
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import TradeInputs


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _signal(strategy: str = "scanner.oversold.v1") -> Signal:
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
        strategy_id=strategy,
        confidence_ai=None,
    )


async def _seed_transition_into(
    mode: TradingMode, days_ago: int, *, from_mode: TradingMode = TradingMode.OFF
) -> None:
    repo = ModeTransitionRepository(async_session_factory)
    when = _now() - timedelta(days=days_ago)
    await repo.add(
        from_mode=from_mode,
        to_mode=mode,
        actor="test",
        reason=None,
        transitioned_at=when,
        override_used=False,
        mode_started_at_after_transition=when,
    )


# ─── OFF -> SHADOW (free) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_to_shadow_free(fresh_db: None) -> None:  # noqa: ARG001
    result = await check_transition_allowed(
        from_mode=TradingMode.OFF,
        to_mode=TradingMode.SHADOW,
        session_factory=async_session_factory,
    )
    assert result.allowed is True


# ─── No-op rejection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_mode_rejected(fresh_db: None) -> None:  # noqa: ARG001
    result = await check_transition_allowed(
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.SHADOW,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason == "no_op_transition"


# ─── SHADOW -> PAPER ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_to_paper_blocked_at_13_days(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_transition_into(TradingMode.SHADOW, days_ago=13)
    result = await check_transition_allowed(
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.PAPER,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "insufficient_time_in_mode" in result.reason
    assert "13d_in_shadow" in result.reason


@pytest.mark.asyncio
async def test_shadow_to_paper_allowed_at_14_days(
    fresh_db: None,  # noqa: ARG001
) -> None:
    # 15 days to be defensively above the threshold given the int floor.
    await _seed_transition_into(TradingMode.SHADOW, days_ago=15)
    result = await check_transition_allowed(
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.PAPER,
        session_factory=async_session_factory,
    )
    assert result.allowed is True


# ─── PAPER -> SEMI_AUTO ──────────────────────────────────────────────


async def _seed_paper_with_trades(
    *, days_ago: int, n_trades: int
) -> None:
    """Seed a transition into PAPER + ``n_trades`` closed trades inside
    the active window.
    """
    await _seed_transition_into(TradingMode.PAPER, days_ago=days_ago)

    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    # Strategy ids must be namespaced + versioned per Signal validation.
    strategies = [f"scanner.bt{i}.v1" for i in range(n_trades)]
    for i in range(n_trades):
        sig = _signal(strategy=strategies[i])
        persisted = await sig_repo.add(sig)
        trade = await trade_repo.add(
            TradeInputs(
                signal_id=persisted.id,
                ticker="BTC/USDT",
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
        await trade_repo.transition(
            trade.trade_id, "closed",
            actor="seed", event_type="closed",
            expected_from_status="open",
            exit_price=Decimal("61000"),
            realized_pnl_quote=Decimal("1.0"),
        )


@pytest.mark.asyncio
async def test_paper_to_semi_auto_blocked_with_30d_but_49_trades(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_paper_with_trades(days_ago=31, n_trades=49)
    result = await check_transition_allowed(
        from_mode=TradingMode.PAPER,
        to_mode=TradingMode.SEMI_AUTO,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "insufficient_closed_trades" in result.reason


@pytest.mark.asyncio
async def test_paper_to_semi_auto_allowed_with_30d_and_50_trades(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_paper_with_trades(days_ago=31, n_trades=50)
    result = await check_transition_allowed(
        from_mode=TradingMode.PAPER,
        to_mode=TradingMode.SEMI_AUTO,
        session_factory=async_session_factory,
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_paper_to_semi_auto_blocked_at_29_days(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_paper_with_trades(days_ago=29, n_trades=50)
    result = await check_transition_allowed(
        from_mode=TradingMode.PAPER,
        to_mode=TradingMode.SEMI_AUTO,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "insufficient_time_in_mode" in result.reason


# ─── SEMI_AUTO -> LIVE (always blocked until FASE 13) ────────────────


@pytest.mark.asyncio
async def test_semi_auto_to_live_blocked_by_clean_streak_placeholder(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """SEMI_AUTO -> LIVE blocked when clean-streak below threshold.

    FASE 13.5: days_clean_streak now reads from critical_incidents.
    A recent severe incident collapses the streak below the 60-day
    threshold and SEMI_AUTO -> LIVE is rejected with the same
    'insufficient_clean_streak' reason as the placeholder did.
    """
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime as _dt

    from mib.observability.incidents import (  # noqa: PLC0415
        CriticalIncidentRepository,
        CriticalIncidentType,
    )

    await _seed_transition_into(TradingMode.SEMI_AUTO, days_ago=120)
    # Seed a severe incident from 5 days ago -> streak collapses to ~5d.
    incident_repo = CriticalIncidentRepository(async_session_factory)
    await incident_repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_dt.now(UTC).replace(tzinfo=None) - timedelta(days=5),
        auto_detected=True,
    )
    result = await check_transition_allowed(
        from_mode=TradingMode.SEMI_AUTO,
        to_mode=TradingMode.LIVE,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "insufficient_clean_streak" in result.reason


@pytest.mark.asyncio
async def test_semi_auto_to_live_blocked_by_time_first(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Time gate fires before the clean-streak gate when both fail."""
    await _seed_transition_into(TradingMode.SEMI_AUTO, days_ago=10)
    result = await check_transition_allowed(
        from_mode=TradingMode.SEMI_AUTO,
        to_mode=TradingMode.LIVE,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "insufficient_time_in_mode" in result.reason


# ─── Backwards / regressions ────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_to_paper_without_reason_rejected(
    fresh_db: None,  # noqa: ARG001
) -> None:
    result = await check_transition_allowed(
        from_mode=TradingMode.LIVE,
        to_mode=TradingMode.PAPER,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason == "regression_requires_reason"


@pytest.mark.asyncio
async def test_live_to_paper_with_reason_allowed(
    fresh_db: None,  # noqa: ARG001
) -> None:
    result = await check_transition_allowed(
        from_mode=TradingMode.LIVE,
        to_mode=TradingMode.PAPER,
        session_factory=async_session_factory,
        reason="incident triage",
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_any_to_off_always_free(fresh_db: None) -> None:  # noqa: ARG001
    """Defensive regression to OFF is always permitted."""
    for src in (
        TradingMode.SHADOW,
        TradingMode.PAPER,
        TradingMode.SEMI_AUTO,
        TradingMode.LIVE,
    ):
        result = await check_transition_allowed(
            from_mode=src,
            to_mode=TradingMode.OFF,
            session_factory=async_session_factory,
        )
        assert result.allowed is True


# ─── Skip-the-ladder paths ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_shadow_to_live_unknown_ladder_path(
    fresh_db: None,  # noqa: ARG001
) -> None:
    result = await check_transition_allowed(
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.LIVE,
        session_factory=async_session_factory,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "unknown_ladder_path" in result.reason


# ─── Helper functions exposed for /mode_status ──────────────────────


@pytest.mark.asyncio
async def test_days_in_current_mode_zero_when_no_transition(
    fresh_db: None,  # noqa: ARG001
) -> None:
    days = await days_in_current_mode(
        TradingMode.SHADOW, async_session_factory
    )
    assert days == 0


@pytest.mark.asyncio
async def test_days_in_current_mode_uses_latest_into(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Re-entry: latest transition into the mode is the anchor."""
    # First entry 100d ago.
    await _seed_transition_into(TradingMode.SHADOW, days_ago=100)
    # Re-entry 5d ago — anchor moves forward.
    await _seed_transition_into(
        TradingMode.SHADOW,
        days_ago=5,
        from_mode=TradingMode.PAPER,
    )
    days = await days_in_current_mode(
        TradingMode.SHADOW, async_session_factory
    )
    assert days == 5


@pytest.mark.asyncio
async def test_closed_trades_in_mode_counts_window(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_paper_with_trades(days_ago=10, n_trades=3)
    count = await closed_trades_in_mode(
        TradingMode.PAPER, async_session_factory
    )
    assert count == 3
