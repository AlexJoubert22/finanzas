"""Tests for the /preflight checklist (FASE 14.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.observability.scheduler_health import (
    _reset_for_tests as _reset_health,
)
from mib.observability.scheduler_health import (
    get_scheduler_health,
)
from mib.operations.preflight import (
    MIN_CLEAN_STREAK_FOR_LIVE,
    MIN_DAYS_IN_PAPER,
    MIN_TRADES_IN_PAPER,
    format_preflight_html,
    run_preflight,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    _reset_health()


async def _seed_state(*, enabled: bool = False) -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                f"VALUES (1, {1 if enabled else 0}, 0.03, 0.25, NULL, 'paper', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


async def _seed_recent_clean_reconcile() -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO reconcile_runs "
                "(started_at, finished_at, status, triggered_by, "
                " orphan_exchange_count, orphan_db_count, "
                " balance_drift_count, discrepancies_json, error_message) "
                "VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'ok', "
                " 'test', 0, 0, 0, '[]', NULL)"
            )
        )


def _mark_scheduler_alive() -> None:
    health = get_scheduler_health()
    health.last_tick_at = _now()
    health.last_reconcile_at = _now()


# ─── Cold-start: most checks fail ───────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_not_ready(fresh_db: None) -> None:  # noqa: ARG001
    """Empty DB + no scheduler tick → not ready."""
    report = await run_preflight()
    assert report.ready is False
    failed = [c.name for c in report.failed_critical]
    # We expect AT LEAST these critical failures on cold-start.
    assert "trading_state" in failed
    assert "scheduler" in failed
    assert "reconcile_clean" in failed


# ─── Per-check unit assertions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_trading_state_passes_when_enabled_false(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(enabled=False)
    report = await run_preflight()
    state_check = next(c for c in report.checks if c.name == "trading_state")
    assert state_check.passed is True


@pytest.mark.asyncio
async def test_trading_state_warns_when_already_enabled(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(enabled=True)
    report = await run_preflight()
    state_check = next(c for c in report.checks if c.name == "trading_state")
    assert state_check.passed is False
    assert state_check.severity == "warning"


@pytest.mark.asyncio
async def test_scheduler_check_after_tick(
    fresh_db: None,  # noqa: ARG001
) -> None:
    _mark_scheduler_alive()
    report = await run_preflight()
    sched = next(c for c in report.checks if c.name == "scheduler")
    assert sched.passed is True


@pytest.mark.asyncio
async def test_scheduler_stalled_fails(
    fresh_db: None,  # noqa: ARG001
) -> None:
    health = get_scheduler_health()
    health.last_tick_at = _now() - timedelta(minutes=5)  # > 90s threshold
    report = await run_preflight()
    sched = next(c for c in report.checks if c.name == "scheduler")
    assert sched.passed is False


@pytest.mark.asyncio
async def test_reconcile_clean_passes_with_recent_ok_run(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_recent_clean_reconcile()
    report = await run_preflight()
    rec = next(c for c in report.checks if c.name == "reconcile_clean")
    assert rec.passed is True


@pytest.mark.asyncio
async def test_clean_streak_passes_at_threshold(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Empty incidents → streak == MAX (365) → passes."""
    report = await run_preflight()
    streak_check = next(
        c for c in report.checks if c.name == "days_clean_streak"
    )
    assert streak_check.passed is True
    assert f"need >= {MIN_CLEAN_STREAK_FOR_LIVE}d" in streak_check.details


@pytest.mark.asyncio
async def test_paper_validation_fails_without_history(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """No mode_transitions, no trades → 0d/0 trades → fail."""
    report = await run_preflight()
    paper = next(c for c in report.checks if c.name == "paper_validation")
    assert paper.passed is False
    assert f"need >= {MIN_DAYS_IN_PAPER}" in paper.details
    assert f"need >= {MIN_TRADES_IN_PAPER}" in paper.details


@pytest.mark.asyncio
async def test_dead_man_fails_when_token_empty(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_token", "")
    report = await run_preflight()
    dm = next(c for c in report.checks if c.name == "dead_man")
    assert dm.passed is False
    assert dm.severity == "critical"


@pytest.mark.asyncio
async def test_dead_man_passes_when_token_set(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_token", "configured")
    report = await run_preflight()
    dm = next(c for c in report.checks if c.name == "dead_man")
    assert dm.passed is True


@pytest.mark.asyncio
async def test_format_html_renders_summary(
    fresh_db: None,  # noqa: ARG001
) -> None:
    report = await run_preflight()
    html = format_preflight_html(report)
    assert "Pre-flight checklist" in html
    if report.ready:
        assert "READY" in html
    else:
        assert "NOT READY" in html
        assert "critical" in html


@pytest.mark.asyncio
async def test_warnings_dont_block_ready(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic: pass every critical, leave warnings flagged → ready=True."""
    # Seed every critical-passing precondition.
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_token", "configured")
    monkeypatch.setattr(settings, "binance_sandbox_api_key", "fake")
    monkeypatch.setattr(settings, "binance_sandbox_secret", "fake")
    await _seed_state(enabled=False)
    await _seed_recent_clean_reconcile()
    _mark_scheduler_alive()

    # Seed PAPER history: 30+ days + 50+ trades.
    repo_imports = """
    """  # placeholder string to silence future linters
    _ = repo_imports
    from datetime import UTC as _UTC  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    from mib.trading.mode import TradingMode  # noqa: PLC0415
    from mib.trading.mode_transitions_repo import (  # noqa: PLC0415
        ModeTransitionRepository,
    )
    from mib.trading.signal_repo import SignalRepository  # noqa: PLC0415
    from mib.trading.signals import Signal  # noqa: PLC0415
    from mib.trading.trade_repo import TradeRepository  # noqa: PLC0415
    from mib.trading.trades import TradeInputs  # noqa: PLC0415

    when = _now() - timedelta(days=35)
    await ModeTransitionRepository(async_session_factory).add(
        from_mode=TradingMode.SHADOW,
        to_mode=TradingMode.PAPER,
        actor="test",
        reason=None,
        transitioned_at=when,
        override_used=False,
        mode_started_at_after_transition=when,
    )

    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    for i in range(50):
        sig = Signal(
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
            generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=_UTC),
            strategy_id=f"scanner.bt{i}.v1",
            confidence_ai=None,
        )
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

    report = await run_preflight()
    # All critical pass; only the FASE 26 backups + capital_bracket
    # are warnings, which don't block ready.
    assert report.ready is True
