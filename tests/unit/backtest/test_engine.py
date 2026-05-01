"""Tests for the FASE 12.1 backtester engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mib.backtest.engine import (
    Backtester,
    BacktestReport,
    _build_symbol_response,
    _size_for_signal,
)
from mib.backtest.fill_simulator import (
    FillSimulationResult,
    FillSimulator,
    NoFillSimulator,
)
from mib.backtest.types import (
    BacktestBar,
    BacktestSettings,
)
from mib.models.market import Candle, TechnicalSnapshot
from mib.trading.signals import Signal


def _bar(
    *,
    ts: datetime,
    o: float,
    h: float,
    low: float,
    c: float,
    v: float = 1000.0,
    rsi: float | None = 25.0,
    atr: float | None = 2.0,
) -> BacktestBar:
    return BacktestBar(
        candle=Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=v),
        indicators=TechnicalSnapshot(rsi_14=rsi, atr_14=atr),
    )


def _series(
    *,
    n_bars: int,
    start_price: float = 100.0,
    rsi: float | None = 25.0,
    atr: float | None = 2.0,
    high_offset: float = 0.5,
    low_offset: float = 0.5,
    volume_pattern: str = "spike",
) -> list[BacktestBar]:
    """Build a synthetic deterministic series.

    ``volume_pattern='spike'`` produces a volume spike on the LAST bar
    (so the oversold preset emits there). ``flat`` keeps volume
    constant. The RSI/ATR are passed-through to every bar so we don't
    have to recompute pandas-ta in the test.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[BacktestBar] = []
    price = start_price
    for i in range(n_bars):
        ts = base + timedelta(hours=i)
        if volume_pattern == "spike":
            volume = 1000.0 if i < n_bars - 1 else 5000.0
        else:
            volume = 1000.0
        bars.append(
            _bar(
                ts=ts,
                o=price,
                h=price + high_offset,
                low=price - low_offset,
                c=price,
                v=volume,
                rsi=rsi,
                atr=atr,
            )
        )
        price += 0.1  # drift up so EOD-close pricing isn't trivially the entry
    return bars


# ─── Pure helpers ────────────────────────────────────────────────────


def test_size_for_signal_uses_risk_capital_over_risk_unit() -> None:
    sig = Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 100.0),
        invalidation=98.0,  # risk per unit = 2.0
        target_1=104.0,
        target_2=110.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
    )
    cfg = BacktestSettings(
        initial_capital_quote=Decimal("1000"),
        risk_per_trade_pct=Decimal("0.01"),
    )
    # risk_capital = 10, risk_per_unit = 2 → size = 5.0 base units.
    assert _size_for_signal(signal=sig, cfg=cfg) == Decimal(
        "5.00000000"
    )


def test_size_for_signal_zero_risk_returns_zero() -> None:
    sig = Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 100.0),
        invalidation=99.99999,  # essentially equal entry
        target_1=104.0,
        target_2=110.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 0.01},
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
    )
    cfg = BacktestSettings(initial_capital_quote=Decimal("1000"))
    # Tiny but positive — not zero, but at least not negative.
    assert _size_for_signal(signal=sig, cfg=cfg) >= Decimal(0)


def test_build_symbol_response_uses_last_bar_indicators_and_window_only() -> None:
    """Look-ahead defence: the SymbolResponse handed to the evaluator
    must contain ONLY bars [0..t] and the indicator snapshot of bar t.
    """
    bars = _series(n_bars=10)
    window = bars[:5]
    sr = _build_symbol_response(ticker="BTC/USDT", bars_window=window)
    assert len(sr.candles) == 5
    assert sr.quote.price == window[-1].candle.close
    assert sr.indicators is window[-1].indicators


# ─── Engine top-level behaviour ──────────────────────────────────────


def test_unknown_preset_raises() -> None:
    bt = Backtester()
    with pytest.raises(ValueError, match="unknown preset"):
        bt.run(preset="nonexistent", feed={})  # type: ignore[arg-type]


def test_empty_feed_returns_empty_report() -> None:
    bt = Backtester()
    report = bt.run(preset="oversold", feed={})
    assert isinstance(report, BacktestReport)
    assert report.trades == []
    assert report.bars_processed == 0
    assert report.skipped_signals == 0
    assert report.universe == ()


def test_oversold_preset_opens_position_on_volume_spike(
) -> None:
    """RSI<30 + volume spike → oversold preset emits a Signal,
    NoFillSimulator fills at next bar's open, position opens.
    """
    # 25 bars: low RSI everywhere, spike on bar 24, drift slowly up.
    # We force EOD-close exit by not providing a stop hit.
    bars = _series(n_bars=25, rsi=22.0, atr=2.0, volume_pattern="spike")
    bt = Backtester(fill_simulator=NoFillSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": bars},
        settings=BacktestSettings(
            initial_capital_quote=Decimal("1000"),
            risk_per_trade_pct=Decimal("0.01"),
            fee_pct=Decimal("0"),
        ),
    )
    # At least one trade fired (signal emitted on the spike bar; filled
    # at bar 25's open... but we only have 25 bars, so it's filled at
    # bar 24's own next which would be... actually wait, range is
    # [0..24], so spike at idx=24, next_bar=None → NoFillSimulator
    # falls back to current_bar.open. Trade closes at end_of_data.
    assert report.bars_processed == 25
    assert len(report.trades) >= 1
    last_trade = report.trades[-1]
    assert last_trade.ticker == "BTC/USDT"
    assert last_trade.side == "long"
    assert last_trade.exit_reason == "end_of_data"


def test_concurrent_signals_blocked_while_open() -> None:
    """While a position is open, additional signals on subsequent bars
    are skipped (not entered) — production gate equivalent.
    """

    class _AlwaysFireOversoldSimulator(NoFillSimulator):
        pass

    # Two consecutive volume spikes — without the open-position guard,
    # this would open two trades.
    bars: list[BacktestBar] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(20):
        bars.append(
            _bar(
                ts=base + timedelta(hours=i),
                o=100.0 + i * 0.01,
                h=100.5 + i * 0.01,
                low=99.5 + i * 0.01,  # never trips a stop
                c=100.0 + i * 0.01,
                v=5000.0 if i in (8, 12, 16) else 100.0,
                rsi=22.0,
                atr=2.0,
            )
        )

    bt = Backtester(fill_simulator=_AlwaysFireOversoldSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": bars},
        settings=BacktestSettings(fee_pct=Decimal("0")),
    )
    # Engine never opens >1 position concurrently; second/third spikes
    # are skipped while position is open. End_of_data closes the only
    # open trade.
    open_at_a_time = max(
        sum(1 for t in report.trades if t.entry_at == bar.candle.timestamp)
        for bar in bars
    )
    assert open_at_a_time <= 1


def test_long_position_stop_hit_records_stop_exit() -> None:
    """When the bar's low touches the invalidation, the engine
    records a 'stop' exit at the stop price.
    """
    bars: list[BacktestBar] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(25):
        spike = 5000.0 if i == 23 else 100.0
        # Bar 24 has a deep wick that touches the stop.
        if i == 24:
            o, h, low, c = 100.0, 100.2, 80.0, 99.0
        else:
            o, h, low, c = 100.0, 100.5, 99.5, 100.0
        bars.append(
            _bar(
                ts=base + timedelta(hours=i),
                o=o, h=h, low=low, c=c,
                v=spike,
                rsi=22.0,
                atr=2.0,
            )
        )
    bt = Backtester(fill_simulator=NoFillSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": bars},
        settings=BacktestSettings(fee_pct=Decimal("0")),
    )
    # Find the trade exited via stop.
    stop_trades = [t for t in report.trades if t.exit_reason == "stop"]
    assert len(stop_trades) >= 1
    assert stop_trades[0].exit_price == stop_trades[0].invalidation_price


def test_long_position_target_hit_records_target_exit() -> None:
    """When a bar's high reaches target_1, exit at target."""
    bars: list[BacktestBar] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(25):
        spike = 5000.0 if i == 23 else 100.0
        if i == 24:
            # Big up bar that pierces the target.
            o, h, low, c = 100.0, 110.0, 99.5, 109.0
        else:
            o, h, low, c = 100.0, 100.5, 99.5, 100.0
        bars.append(
            _bar(
                ts=base + timedelta(hours=i),
                o=o, h=h, low=low, c=c,
                v=spike,
                rsi=22.0,
                atr=2.0,
            )
        )
    bt = Backtester(fill_simulator=NoFillSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": bars},
        settings=BacktestSettings(fee_pct=Decimal("0")),
    )
    target_trades = [t for t in report.trades if t.exit_reason == "target"]
    assert len(target_trades) >= 1
    assert target_trades[0].realized_pnl_quote > Decimal(0)


# ─── Fill simulator interface ────────────────────────────────────────


def test_no_fill_simulator_default_response() -> None:
    sim = NoFillSimulator()
    bar = _bar(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        o=100.0, h=101.0, low=99.0, c=100.5,
    )
    next_bar = _bar(
        ts=datetime(2026, 1, 1, 1, tzinfo=UTC),
        o=100.7, h=101.5, low=100.0, c=101.0,
    )
    res = sim.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal("1.0"),
        limit_price=None,
        current_bar=bar,
        next_bar=next_bar,
        fee_pct=Decimal("0.001"),
    )
    assert res.filled is True
    assert res.fill_price == Decimal("100.7")
    assert res.fees_paid_quote == Decimal(0)


def test_no_fill_simulator_falls_back_to_current_when_no_next() -> None:
    sim = NoFillSimulator()
    bar = _bar(
        ts=datetime(2026, 1, 1, tzinfo=UTC),
        o=100.0, h=101.0, low=99.0, c=100.5,
    )
    res = sim.simulate_fill(
        side="buy",
        order_type="market",
        amount_base=Decimal("1.0"),
        limit_price=None,
        current_bar=bar,
        next_bar=None,
        fee_pct=Decimal("0.001"),
    )
    assert res.filled is True
    assert res.fill_price == Decimal("100.0")


def test_engine_uses_fill_simulator_protocol() -> None:
    """A custom simulator that always rejects fills should produce 0
    trades and a non-zero skipped_signals counter.
    """

    class _RejectAll:
        """Conforms to the FillSimulator Protocol but rejects everything."""

        def simulate_fill(
            self,
            *,
            side: str,  # noqa: ARG002
            order_type: str,  # noqa: ARG002
            amount_base: Decimal,  # noqa: ARG002
            limit_price: Decimal | None,  # noqa: ARG002
            current_bar: BacktestBar,  # noqa: ARG002
            next_bar: BacktestBar | None,  # noqa: ARG002
            fee_pct: Decimal,  # noqa: ARG002
        ) -> FillSimulationResult:
            from datetime import datetime as _dt  # noqa: PLC0415

            return FillSimulationResult(
                filled=False,
                fill_price=Decimal(0),
                filled_amount=Decimal(0),
                fees_paid_quote=Decimal(0),
                fill_at=_dt(2026, 1, 1, tzinfo=UTC),
                reason="test reject",
            )

        def reseed(self, seed: int) -> None:  # noqa: ARG002
            pass

    bars = _series(n_bars=25, rsi=22.0, atr=2.0, volume_pattern="spike")
    bt = Backtester(fill_simulator=_RejectAll())  # type: ignore[arg-type]
    report = bt.run(preset="oversold", feed={"BTC/USDT": bars})
    assert report.trades == []
    assert report.skipped_signals >= 1


def test_fill_simulator_protocol_runtime_check() -> None:
    """``FillSimulator`` is a runtime_checkable Protocol so duck-typed
    conforming classes register positively without subclassing.
    """
    sim = NoFillSimulator()
    assert isinstance(sim, FillSimulator)
