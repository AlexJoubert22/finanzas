"""Tests for /paper_status snapshot + renderer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from mib.db.session import async_session_factory
from mib.telegram.handlers.paper_status import (
    PaperStatusSnapshot,
    build_paper_snapshot,
    render_paper_snapshot,
)
from mib.trading.mode import TradingMode


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_state(mode: str = "paper") -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                f"VALUES (1, 1, 0.03, 0.25, NULL, '{mode}', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


async def _seed_mode_transition_into_paper(*, days_ago: int) -> None:
    """Insert an audit row showing PAPER was entered ``days_ago`` ago."""
    started = _now() - timedelta(days=days_ago)
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO mode_transitions "
                "(from_mode, to_mode, actor, reason, transitioned_at, "
                " override_used, mode_started_at_after_transition) "
                "VALUES ('shadow', 'paper', 'test', 'seed', :ts, 0, :ts)"
            ),
            {"ts": started},
        )


async def _seed_paper_trade(*, pnl: Decimal, closed_at: datetime) -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO signals "
                "(ticker, side, strength, timeframe, entry_low, entry_high, "
                " invalidation, target_1, target_2, rationale, indicators_json, "
                " generated_at, strategy_id, status, status_updated_at) "
                "VALUES ('BTC/USDT', 'long', 0.7, '1h', 100, 101, 97, 103, 109, "
                " 'seed', '{}', CURRENT_TIMESTAMP, 'scanner.x.v1', "
                " 'pending', CURRENT_TIMESTAMP)"
            )
        )
        sid = (
            await session.execute(text("SELECT last_insert_rowid()"))
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO trades "
                "(signal_id, ticker, side, size, entry_price, "
                " stop_loss_price, opened_at, closed_at, status, "
                " realized_pnl_quote, fees_paid_quote, exchange_id) "
                "VALUES (:sid, 'BTC/USDT', 'long', 0.001, 60000, 58800, "
                " :opened, :closed, 'closed', :pnl, 0, 'test-ex-id')"
            ),
            {
                "sid": sid,
                "opened": closed_at - timedelta(hours=1),
                "closed": closed_at,
                "pnl": str(pnl),
            },
        )


# ─── Renderer ────────────────────────────────────────────────────────


def test_render_paper_mode_header() -> None:
    snap = PaperStatusSnapshot(
        mode=TradingMode.PAPER,
        baseline_quote=Decimal("6000"),
        equity_quote=Decimal("5800"),
        cumulative_pnl=Decimal("-200"),
        days_in_paper=5,
        closed_trades=12,
        wins=6,
        losses=4,
        realized_sharpe=None,
        days_to_next_threshold=25,
        trades_to_next_threshold=38,
    )
    out = render_paper_snapshot(snap)
    assert "🎮" in out
    assert "/paper_status" in out
    assert "<code>6000</code> USDT" in out
    assert "🔴" in out  # negative PnL marker
    assert "(-3.33%)" in out  # -200/6000 = -3.33%
    assert "win-rate: <code>60.0%</code>" in out
    assert "Sharpe" in out
    assert "🔒 SEMI_AUTO bloqueado" in out


def test_render_warns_when_not_in_paper() -> None:
    snap = PaperStatusSnapshot(
        mode=TradingMode.SHADOW,
        baseline_quote=Decimal("6000"),
        equity_quote=None,
        cumulative_pnl=Decimal(0),
        days_in_paper=0,
        closed_trades=0,
        wins=0,
        losses=0,
        realized_sharpe=None,
        days_to_next_threshold=30,
        trades_to_next_threshold=50,
    )
    out = render_paper_snapshot(snap)
    assert "⚠️" in out
    assert "modo actual NO es PAPER" in out


def test_render_unlocks_semi_auto_when_thresholds_met() -> None:
    snap = PaperStatusSnapshot(
        mode=TradingMode.PAPER,
        baseline_quote=Decimal("6000"),
        equity_quote=Decimal("6500"),
        cumulative_pnl=Decimal("500"),
        days_in_paper=35,
        closed_trades=60,
        wins=40,
        losses=20,
        realized_sharpe=1.42,
        days_to_next_threshold=0,
        trades_to_next_threshold=0,
    )
    out = render_paper_snapshot(snap)
    assert "✅ <b>SEMI_AUTO desbloqueado</b>" in out
    assert "<code>1.42</code>" in out  # Sharpe rendered


# ─── Snapshot builder (DB-backed) ────────────────────────────────────


@pytest.mark.asyncio
async def test_build_snapshot_no_paper_history(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(mode="shadow")
    snap = await build_paper_snapshot(session_factory=async_session_factory)
    assert snap.mode == TradingMode.SHADOW
    assert snap.days_in_paper == 0
    assert snap.closed_trades == 0
    assert snap.cumulative_pnl == Decimal(0)
    assert snap.realized_sharpe is None
    assert snap.days_to_next_threshold == 30
    assert snap.trades_to_next_threshold == 50


@pytest.mark.asyncio
async def test_build_snapshot_aggregates_paper_pnls(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(mode="paper")
    await _seed_mode_transition_into_paper(days_ago=10)
    # Within PAPER window.
    await _seed_paper_trade(
        pnl=Decimal("50"), closed_at=_now() - timedelta(days=2)
    )
    await _seed_paper_trade(
        pnl=Decimal("-20"), closed_at=_now() - timedelta(days=1)
    )
    # Outside PAPER window (predates the transition).
    await _seed_paper_trade(
        pnl=Decimal("999"), closed_at=_now() - timedelta(days=20)
    )

    snap = await build_paper_snapshot(session_factory=async_session_factory)
    assert snap.mode == TradingMode.PAPER
    assert snap.days_in_paper == 10
    assert snap.closed_trades == 2  # only PAPER-window trades count
    assert snap.cumulative_pnl == Decimal("30")
    assert snap.wins == 1
    assert snap.losses == 1
    # 2 trades < SHARPE_MIN_TRADES → no Sharpe.
    assert snap.realized_sharpe is None


@pytest.mark.asyncio
async def test_build_snapshot_thresholds_progress(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(mode="paper")
    await _seed_mode_transition_into_paper(days_ago=10)
    snap = await build_paper_snapshot(session_factory=async_session_factory)
    assert snap.days_to_next_threshold == 20
    assert snap.trades_to_next_threshold == 50
    assert not snap.can_advance_to_semi_auto
