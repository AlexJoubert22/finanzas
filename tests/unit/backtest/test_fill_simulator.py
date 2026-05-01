"""Tests for :class:`SlippageFillSimulator` (FASE 12.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mib.backtest.fill_simulator import (
    SlippageConfig,
    SlippageFillSimulator,
    _apply_slippage,
)
from mib.backtest.types import BacktestBar
from mib.models.market import Candle, TechnicalSnapshot


def _bar(
    *, ts: datetime, o: float, h: float, low: float, c: float, v: float = 1000.0
) -> BacktestBar:
    return BacktestBar(
        candle=Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=v),
        indicators=TechnicalSnapshot(),
    )


def _t(i: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)


# ─── _apply_slippage pure ────────────────────────────────────────────


def test_apply_slippage_buy_increases_price() -> None:
    fill = _apply_slippage(mid=Decimal(100), side="buy", bps=Decimal(5))
    # 100 * (1 + 0.0005) = 100.05
    assert fill == Decimal("100.05")


def test_apply_slippage_sell_decreases_price() -> None:
    fill = _apply_slippage(mid=Decimal(100), side="sell", bps=Decimal(5))
    # 100 / 1.0005 ≈ 99.95002...
    assert fill < Decimal(100)
    assert fill > Decimal("99.94")


# ─── Market orders ───────────────────────────────────────────────────


def test_market_buy_fills_with_slippage_at_next_open() -> None:
    sim = SlippageFillSimulator(
        SlippageConfig(
            fixed_bps=Decimal("5"),
            market_impact_coefficient=Decimal(0),
        ),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100, v=1000)
    nxt = _bar(ts=_t(1), o=100, h=102, low=99, c=101, v=1000)
    res = sim.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal(1),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is True
    # Fill price > mid (100) by exactly fixed_bps with impact=0.
    assert res.fill_price > Decimal(100)
    assert res.fill_price == Decimal("100.05000000")
    assert res.fill_at == _t(1)
    assert res.slippage_bps_applied == Decimal("5")


def test_market_sell_fills_below_mid() -> None:
    sim = SlippageFillSimulator(
        SlippageConfig(fixed_bps=Decimal("5"), market_impact_coefficient=Decimal(0)),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=100, h=102, low=99, c=101)
    res = sim.simulate_fill(
        side="sell",
        order_type="market",
        amount_base=Decimal(1),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is True
    assert res.fill_price < Decimal(100)


def test_market_impact_increases_slippage_for_large_notional() -> None:
    """impact_bps = coef * notional / avg_volume_per_min."""
    sim_small = SlippageFillSimulator(
        SlippageConfig(
            fixed_bps=Decimal("5"),
            market_impact_coefficient=Decimal("0.1"),
        ),
        seed=0,
    )
    sim_big = SlippageFillSimulator(
        SlippageConfig(
            fixed_bps=Decimal("5"),
            market_impact_coefficient=Decimal("0.1"),
        ),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100, v=600)  # 10/min
    nxt = _bar(ts=_t(1), o=100, h=102, low=99, c=101, v=600)
    small = sim_small.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal("0.1"),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    big = sim_big.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal("100"),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert big.fill_price > small.fill_price
    assert big.slippage_bps_applied is not None
    assert small.slippage_bps_applied is not None
    assert big.slippage_bps_applied > small.slippage_bps_applied


def test_volume_zero_forces_impact_to_zero(caplog: pytest.LogCaptureFixture) -> None:
    sim = SlippageFillSimulator(
        SlippageConfig(
            fixed_bps=Decimal("5"),
            market_impact_coefficient=Decimal("0.1"),
        ),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100, v=0.0)
    nxt = _bar(ts=_t(1), o=100, h=102, low=99, c=101, v=0.0)
    with caplog.at_level("WARNING"):
        res = sim.simulate_fill(
            side="buy",
            order_type="market",
            amount_base=Decimal("100"),
            limit_price=None,
            current_bar=cur,
            next_bar=nxt,
            fee_pct=Decimal("0"),
        )
    # Impact contribution is 0; only fixed_bps remains.
    assert res.slippage_bps_applied == Decimal("5")
    assert any("volume=0" in rec.message for rec in caplog.records)


def test_market_with_fee_pct_charges_fees() -> None:
    sim = SlippageFillSimulator(
        SlippageConfig(fixed_bps=Decimal(0), market_impact_coefficient=Decimal(0)),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=100, h=102, low=99, c=101)
    res = sim.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal("2"),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0.001"),  # 10 bps
    )
    # 100 * 2 * 0.001 = 0.2
    assert res.fees_paid_quote == Decimal("0.20000000")


# ─── Limit orders ────────────────────────────────────────────────────


def test_limit_buy_crosses_and_fills_with_lucky_seed() -> None:
    """Find a seed where the RNG roll is well above no_fill_probability."""
    sim = SlippageFillSimulator(
        SlippageConfig(
            limit_no_fill_probability=Decimal("0.30"),
            fixed_bps=Decimal(0),
        ),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    # Next bar dips to 98 → buy limit at 99 crosses.
    nxt = _bar(ts=_t(1), o=99.5, h=100, low=98, c=99)
    # Probe seeds; pick one whose first random() > 0.30.
    for s in range(10):
        sim.reseed(s)
        res = sim.simulate_fill(
            side="buy",
            order_type="limit",
            amount_base=Decimal(1),
            limit_price=Decimal(99),
            current_bar=cur,
            next_bar=nxt,
            fee_pct=Decimal("0"),
        )
        if res.filled:
            assert res.fill_price == Decimal("99.00000000")
            assert res.slippage_bps_applied == Decimal(0)
            return
    pytest.fail("no seed in [0, 10) produced a filled limit")


def test_limit_buy_no_cross_no_fill() -> None:
    sim = SlippageFillSimulator(SlippageConfig(), seed=0)
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=100, h=101, low=99.5, c=100)
    res = sim.simulate_fill(
        side="buy",
        order_type="limit",
        amount_base=Decimal(1),
        limit_price=Decimal("99"),  # below next.low
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is False
    assert res.reason == "limit_did_not_cross"


def test_limit_buy_random_no_fill_when_probability_is_one() -> None:
    """no_fill_probability=1.0 → always rejects even when crossed."""
    sim = SlippageFillSimulator(
        SlippageConfig(limit_no_fill_probability=Decimal("1.0")),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=99, h=100, low=98, c=99)
    res = sim.simulate_fill(
        side="buy",
        order_type="limit",
        amount_base=Decimal(1),
        limit_price=Decimal(99),
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is False
    assert res.reason == "limit_no_fill_random"


# ─── Stop orders ─────────────────────────────────────────────────────


def test_stop_long_triggered_fills_with_extra_slippage() -> None:
    sim = SlippageFillSimulator(
        SlippageConfig(
            fixed_bps=Decimal("5"),
            stop_extra_bps_multiplier=Decimal("1.5"),
            market_impact_coefficient=Decimal(0),
        ),
        seed=0,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    # Next bar wicks to 95 → sell-stop at 96 triggered.
    nxt = _bar(ts=_t(1), o=99, h=100, low=95, c=97)
    res = sim.simulate_fill(
        side="sell",
        order_type="stop_market",
        amount_base=Decimal(1),
        limit_price=Decimal(96),
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is True
    # Sell stop slips below the stop trigger by 7.5 bps (5 * 1.5).
    assert res.slippage_bps_applied == Decimal("7.5")
    assert res.fill_price < Decimal(96)


def test_stop_long_not_triggered_returns_no_fill() -> None:
    sim = SlippageFillSimulator(SlippageConfig(), seed=0)
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=99.5, h=100, low=99, c=99.5)  # never touches 96
    res = sim.simulate_fill(
        side="sell",
        order_type="stop_market",
        amount_base=Decimal(1),
        limit_price=Decimal(96),
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is False
    assert res.reason == "stop_not_hit"


# ─── Reproducibility (CRITICAL) ──────────────────────────────────────


def test_reproducibility_same_seed_same_outcomes() -> None:
    """Same seed + same calls -> byte-identical FillSimulationResults."""
    config = SlippageConfig(limit_no_fill_probability=Decimal("0.30"))
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=99, h=100, low=98, c=99)

    def _run() -> list[bool]:
        sim = SlippageFillSimulator(config, seed=42)
        outcomes: list[bool] = []
        for _ in range(20):
            res = sim.simulate_fill(
                side="buy",
                order_type="limit",
                amount_base=Decimal(1),
                limit_price=Decimal(99),
                current_bar=cur,
                next_bar=nxt,
                fee_pct=Decimal("0"),
            )
            outcomes.append(res.filled)
        return outcomes

    a = _run()
    b = _run()
    assert a == b


def test_reseed_resets_rng_state() -> None:
    """``reseed(seed)`` brings the RNG back to the same starting state."""
    sim = SlippageFillSimulator(
        SlippageConfig(limit_no_fill_probability=Decimal("0.30")),
        seed=7,
    )
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=99, h=100, low=98, c=99)

    def _take_n(n: int) -> list[bool]:
        return [
            sim.simulate_fill(
                side="buy",
                order_type="limit",
                amount_base=Decimal(1),
                limit_price=Decimal(99),
                current_bar=cur,
                next_bar=nxt,
                fee_pct=Decimal("0"),
            ).filled
            for _ in range(n)
        ]

    first_run = _take_n(10)
    sim.reseed(7)
    second_run = _take_n(10)
    assert first_run == second_run


# ─── Defensive paths ─────────────────────────────────────────────────


def test_limit_without_price_returns_no_fill() -> None:
    sim = SlippageFillSimulator(SlippageConfig(), seed=0)
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    nxt = _bar(ts=_t(1), o=99, h=100, low=98, c=99)
    res = sim.simulate_fill(
        side="buy",
        order_type="limit",
        amount_base=Decimal(1),
        limit_price=None,
        current_bar=cur,
        next_bar=nxt,
        fee_pct=Decimal("0"),
    )
    assert res.filled is False
    assert res.reason == "limit_price_missing"


def test_limit_without_next_bar_returns_no_fill() -> None:
    sim = SlippageFillSimulator(SlippageConfig(), seed=0)
    cur = _bar(ts=_t(0), o=100, h=101, low=99, c=100)
    res = sim.simulate_fill(
        side="buy",
        order_type="limit",
        amount_base=Decimal(1),
        limit_price=Decimal(99),
        current_bar=cur,
        next_bar=None,
        fee_pct=Decimal("0"),
    )
    assert res.filled is False
    assert res.reason == "no_next_bar_for_limit"
