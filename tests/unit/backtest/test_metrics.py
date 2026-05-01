"""Tests for :mod:`mib.backtest.metrics` (FASE 12.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from mib.backtest.metrics import (
    INFINITY_SENTINEL,
    _decimal_sqrt,
    _r_multiple,
    compute_metrics,
)
from mib.backtest.types import BacktestTrade


def _trade(
    *,
    pnl: str,
    side: str = "long",
    strategy: str = "scanner.oversold.v1",
    ticker: str = "BTC/USDT",
    entry: str = "100",
    exit_: str = "105",
    invalidation: str = "97",
    fees: str = "0",
) -> BacktestTrade:
    base = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    return BacktestTrade(
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        strategy_id=strategy,
        size_base=Decimal("1"),
        entry_price=Decimal(entry),
        entry_at=base,
        exit_price=Decimal(exit_),
        exit_at=base,
        exit_reason="target",
        realized_pnl_quote=Decimal(pnl),
        fees_paid_quote=Decimal(fees),
        invalidation_price=Decimal(invalidation),
        target_1_price=Decimal("110"),
        target_2_price=None,
    )


# ─── Pure helpers ────────────────────────────────────────────────────


def test_decimal_sqrt_basic_cases() -> None:
    assert _decimal_sqrt(Decimal(0)) == Decimal(0)
    assert _decimal_sqrt(Decimal(4)) == Decimal(2)
    # 2 → ~1.41421356...
    s = _decimal_sqrt(Decimal(2))
    assert s > Decimal("1.4142")
    assert s < Decimal("1.4143")


def test_r_multiple_long_winner() -> None:
    t = _trade(entry="100", exit_="106", invalidation="97", pnl="6")
    # gross = 6, risk_per_unit = 3, R = 2
    assert _r_multiple(t) == Decimal("2.00000000")


def test_r_multiple_short_winner() -> None:
    t = _trade(
        side="short",
        entry="100",
        exit_="94",
        invalidation="103",
        pnl="6",
    )
    # short: gross = entry - exit = 6; risk = 3; R = 2
    assert _r_multiple(t) == Decimal("2.00000000")


def test_r_multiple_zero_risk_returns_zero() -> None:
    t = _trade(entry="100", exit_="105", invalidation="100", pnl="5")
    assert _r_multiple(t) == Decimal(0)


# ─── compute_metrics happy paths ─────────────────────────────────────


def test_three_winners_two_losers_metrics() -> None:
    trades = [
        _trade(pnl="10", exit_="110"),
        _trade(pnl="10", exit_="110"),
        _trade(pnl="10", exit_="110"),
        _trade(pnl="-5", exit_="95"),
        _trade(pnl="-5", exit_="95"),
    ]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.total_trades == 5
    assert m.winners == 3
    assert m.losers == 2
    assert m.win_rate == Decimal("0.60000000")
    # PF = 30 / 10 = 3.0
    assert m.profit_factor == Decimal("3.00000000")
    assert m.total_pnl == Decimal(20)


def test_all_winners_returns_infinity_sentinel() -> None:
    trades = [_trade(pnl="5") for _ in range(3)]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.profit_factor == INFINITY_SENTINEL
    assert m.losers == 0


def test_all_losers_profit_factor_zero() -> None:
    trades = [_trade(pnl="-5") for _ in range(3)]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.profit_factor == Decimal(0)
    assert m.winners == 0


def test_empty_trades_returns_zero_metrics() -> None:
    m = compute_metrics([], initial_capital=Decimal(1000))
    assert m.total_trades == 0
    assert m.profit_factor == Decimal(0)
    assert m.sharpe_ratio == Decimal(0)
    assert m.r_distribution == {
        "<-2R": 0, "-2R..-1R": 0, "-1R..0R": 0,
        "0R..1R": 0, "1R..2R": 0, ">=2R": 0,
    }


# ─── Sharpe / Sortino ────────────────────────────────────────────────


def test_sharpe_with_zero_std_returns_zero() -> None:
    """Constant returns → std=0 → Sharpe=0 (no NaN, no Infinity)."""
    trades = [_trade(pnl="5") for _ in range(10)]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    # All returns identical → std=0 → sharpe=0.
    assert m.sharpe_ratio == Decimal(0)


def test_sortino_only_penalises_downside() -> None:
    """Sortino is computed from downside deviation only.
    Two trade sets with identical means but one with bigger upside
    swings should both have similar Sortinos when downside is the same.
    """
    base_pnls = ["-5", "10", "10"]
    big_upside = ["-5", "10", "100"]
    a = compute_metrics(
        [_trade(pnl=p) for p in base_pnls], initial_capital=Decimal(1000)
    )
    b = compute_metrics(
        [_trade(pnl=p) for p in big_upside], initial_capital=Decimal(1000)
    )
    # Both Sortinos > 0 (positive expectancy on both); b's Sortino is
    # higher because mean grew but downside didn't.
    assert a.sortino_ratio > Decimal(0)
    assert b.sortino_ratio > a.sortino_ratio


def test_sortino_no_downside_returns_infinity_sentinel() -> None:
    trades = [_trade(pnl="5") for _ in range(3)]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.sortino_ratio == INFINITY_SENTINEL


# ─── R-distribution ──────────────────────────────────────────────────


def test_r_distribution_classifies_correctly() -> None:
    """Manually crafted trades to land each one in a specific bin.

    risk_per_unit = 3 (entry=100, invalidation=97). Targets per bin:
      <-2R: pnl < -6  → exit < 94
      -2R..-1R: -6..-3 → exit 94..97
      -1R..0R: -3..0  → exit 97..100
      0R..1R: 0..3    → exit 100..103
      1R..2R: 3..6    → exit 103..106
      >=2R: >=6       → exit >= 106
    """
    trades = [
        _trade(pnl="-9", exit_="91"),    # <-2R
        _trade(pnl="-4", exit_="96"),    # -2R..-1R
        _trade(pnl="-1", exit_="99"),    # -1R..0R
        _trade(pnl="2", exit_="102"),    # 0R..1R
        _trade(pnl="4", exit_="104"),    # 1R..2R
        _trade(pnl="9", exit_="109"),    # >=2R
    ]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.r_distribution["<-2R"] == 1
    assert m.r_distribution["-2R..-1R"] == 1
    assert m.r_distribution["-1R..0R"] == 1
    assert m.r_distribution["0R..1R"] == 1
    assert m.r_distribution["1R..2R"] == 1
    assert m.r_distribution[">=2R"] == 1


# ─── Breakdowns ──────────────────────────────────────────────────────


def test_per_strategy_breakdown_independent_metrics() -> None:
    trades = [
        _trade(pnl="10", strategy="scanner.oversold.v1"),
        _trade(pnl="10", strategy="scanner.oversold.v1"),
        _trade(pnl="-5", strategy="scanner.breakout.v1"),
    ]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert "scanner.oversold.v1" in m.per_strategy
    assert "scanner.breakout.v1" in m.per_strategy
    assert m.per_strategy["scanner.oversold.v1"].total_pnl == Decimal(20)
    assert m.per_strategy["scanner.breakout.v1"].total_pnl == Decimal(-5)
    # Recursive descent prevented: sub-breakdowns are empty.
    assert m.per_strategy["scanner.oversold.v1"].per_strategy == {}


def test_per_ticker_breakdown_independent_metrics() -> None:
    trades = [
        _trade(pnl="10", ticker="BTC/USDT"),
        _trade(pnl="-5", ticker="ETH/USDT"),
    ]
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.per_ticker["BTC/USDT"].winners == 1
    assert m.per_ticker["ETH/USDT"].losers == 1


# ─── Drawdown ────────────────────────────────────────────────────────


def test_max_drawdown_captures_peak_to_trough() -> None:
    """+20, +20 (peak=40), -30, +5 → drawdown peak-to-trough = 30."""
    trades = [
        _trade(pnl="20"),
        _trade(pnl="20"),
        _trade(pnl="-30"),
        _trade(pnl="5"),
    ]
    m = compute_metrics(trades, initial_capital=Decimal(100))
    # Trough cum = 100 + 20 + 20 - 30 = 110; peak was 140; dd_abs = 30.
    assert m.max_drawdown_abs == Decimal("30.00000000")
    # Pct relative to peak (140) = 30/140 ≈ 0.21428...
    assert m.max_drawdown_pct > Decimal("0.21")
    assert m.max_drawdown_pct < Decimal("0.22")


def test_expectancy_per_trade() -> None:
    """3 winners @ +10, 2 losers @ -5 →
    win_rate=0.6, loss_rate=0.4, avg_win=10, avg_loss_abs=5,
    expectancy = 0.6*10 - 0.4*5 = 4.
    """
    trades = (
        [_trade(pnl="10") for _ in range(3)]
        + [_trade(pnl="-5") for _ in range(2)]
    )
    m = compute_metrics(trades, initial_capital=Decimal(1000))
    assert m.expectancy == Decimal("4.00000000")
