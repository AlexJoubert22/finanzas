"""Tests for :mod:`mib.backtest.equity` (FASE 12.4)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from mib.backtest.equity import build_equity_curve
from mib.backtest.types import BacktestTrade


def _t(i: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0) + timedelta(hours=i)


def _trade(*, pnl: str, fees: str = "0", exit_h: int) -> BacktestTrade:
    return BacktestTrade(
        ticker="BTC/USDT",
        side="long",
        strategy_id="scanner.oversold.v1",
        size_base=Decimal(1),
        entry_price=Decimal(100),
        entry_at=_t(0),
        exit_price=Decimal("100") + Decimal(pnl),
        exit_at=_t(exit_h),
        exit_reason="target",
        realized_pnl_quote=Decimal(pnl),
        fees_paid_quote=Decimal(fees),
        invalidation_price=Decimal(97),
        target_1_price=Decimal(110),
        target_2_price=None,
    )


def test_no_trades_no_bars_returns_anchor_only() -> None:
    pts = build_equity_curve(initial_capital=Decimal(1000), trades=[])
    assert len(pts) == 1
    assert pts[0].equity_with_fees == Decimal(1000)
    assert pts[0].equity_without_fees == Decimal(1000)


def test_no_trades_with_bars_emits_flat_curve() -> None:
    bars = [_t(i) for i in range(5)]
    pts = build_equity_curve(
        initial_capital=Decimal(1000), trades=[], bar_timestamps=bars
    )
    # Anchor + one point per bar.
    assert len(pts) == 6
    for p in pts:
        assert p.equity_with_fees == Decimal(1000)
        assert p.equity_without_fees == Decimal(1000)


def test_linear_winner_curve_ascends() -> None:
    trades = [
        _trade(pnl="10", exit_h=1),
        _trade(pnl="10", exit_h=2),
        _trade(pnl="10", exit_h=3),
    ]
    pts = build_equity_curve(initial_capital=Decimal(1000), trades=trades)
    eq_series = [p.equity_with_fees for p in pts]
    # Anchor + 3 trades = 4 points; strictly increasing.
    assert len(pts) == 4
    for a, b in zip(eq_series, eq_series[1:], strict=False):
        assert b > a
    assert pts[-1].equity_with_fees == Decimal("1030.00000000")


def test_drawdown_intermediate_low_visible() -> None:
    trades = [
        _trade(pnl="20", exit_h=1),
        _trade(pnl="-30", exit_h=2),
        _trade(pnl="5", exit_h=3),
    ]
    pts = build_equity_curve(initial_capital=Decimal(1000), trades=trades)
    equities = [p.equity_with_fees for p in pts]
    low_point = min(equities)
    # 1000 + 20 - 30 = 990 (after second trade).
    assert low_point == Decimal("990.00000000")


def test_with_fees_always_lte_without_fees() -> None:
    trades = [
        _trade(pnl="10", fees="2", exit_h=1),
        _trade(pnl="-5", fees="1", exit_h=2),
    ]
    pts = build_equity_curve(initial_capital=Decimal(1000), trades=trades)
    for p in pts:
        assert p.equity_with_fees <= p.equity_without_fees


def test_initial_state_equality() -> None:
    pts = build_equity_curve(
        initial_capital=Decimal(1000),
        trades=[_trade(pnl="10", exit_h=1)],
    )
    # Anchor point has equal with/without fees.
    anchor = pts[0]
    assert anchor.equity_with_fees == anchor.equity_without_fees == Decimal(1000)
    assert anchor.realized_pnl_cumulative == Decimal(0)
    assert anchor.fees_cumulative == Decimal(0)


def test_bar_timestamps_emit_flat_between_trades() -> None:
    """Bar timestamps emit a sample at every bar; equity is flat
    between trade closes (no new realized PnL)."""
    trades = [_trade(pnl="10", fees="1", exit_h=2)]
    bars = [_t(0), _t(1), _t(2), _t(3), _t(4)]
    pts = build_equity_curve(
        initial_capital=Decimal(1000),
        trades=trades,
        bar_timestamps=bars,
    )
    # Anchor + 5 bar samples = 6 points.
    assert len(pts) == 6
    # Bar 0 + bar 1: still flat at 1000 (trade hasn't closed yet).
    assert pts[1].equity_with_fees == Decimal(1000)
    assert pts[2].equity_with_fees == Decimal(1000)
    # Bar 2 onwards: trade closed, equity jumps to 1009.
    assert pts[3].equity_with_fees == Decimal("1009.00000000")
    assert pts[4].equity_with_fees == Decimal("1009.00000000")
    assert pts[5].equity_with_fees == Decimal("1009.00000000")
