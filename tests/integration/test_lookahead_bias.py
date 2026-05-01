"""Look-ahead bias guardian (FASE 12.8) — MANDATORY in CI.

This is the canary test that locks in the cardinal rule of the
backtester: at iteration t, the strategy evaluator must see ONLY
data from bars [0, t]. If a future-bar leak ever creeps into the
:func:`mib.backtest.engine._build_symbol_response` (or anywhere it
flows through), this test fails LOUD and FASE 12 is broken.

The guardian is a structural check, not a value comparison:

1. Build a synthetic 100-bar feed.
2. Instrument the StrategyEngine evaluator: replace
   ``evaluate_oversold`` with a recorder that captures the LENGTH of
   ``data.candles`` at each call (and the indicator snapshot's
   identity).
3. Run the backtester over the feed.
4. Assert: at the i-th evaluator call, the candles list has length
   i+1 (window [0..i]) and the indicator snapshot is the SAME object
   as the i-th BacktestBar's ``indicators``.

Any drift here means the engine is peeking past bar t and the test
fails immediately. This locks the contract.

Marked ``@pytest.mark.bias_check`` so a CI job can target it
specifically (in addition to running it as part of the normal suite).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mib.backtest import engine as engine_mod
from mib.backtest.engine import Backtester
from mib.backtest.fill_simulator import NoFillSimulator
from mib.backtest.types import BacktestBar, BacktestSettings
from mib.models.market import Candle, TechnicalSnapshot

pytestmark = pytest.mark.bias_check


def _make_feed(n: int = 100) -> list[BacktestBar]:
    """Synthetic deterministic 100-bar series with a per-bar unique
    indicator object so the test can verify object identity."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[BacktestBar] = []
    price = 100.0
    for i in range(n):
        # Each bar gets its own TechnicalSnapshot instance — identity
        # comparison in the assertion would fail if the engine ever
        # pulled an indicator snapshot from a different bar.
        snap = TechnicalSnapshot(rsi_14=22.0 + i * 0.001, atr_14=2.0)
        bars.append(
            BacktestBar(
                candle=Candle(
                    timestamp=base + timedelta(hours=i),
                    open=price,
                    high=price + 0.5,
                    low=price - 0.5,
                    close=price,
                    volume=5000.0 if i % 10 == 9 else 1000.0,
                ),
                indicators=snap,
            )
        )
        price += 0.1
    return bars


def test_no_lookahead_bias_in_strategy_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """GUARDIAN. The evaluator must never see bars past iteration index."""
    feed = _make_feed(n=100)
    expected_indicators = [b.indicators for b in feed]

    # Each call records (len_candles, indicator_id, timestamp_at_close).
    calls: list[tuple[int, int, datetime]] = []

    def _recorder(data, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(
            (
                len(data.candles),
                id(data.indicators),
                data.candles[-1].timestamp,
            )
        )
        # Signal=None → engine doesn't open a position; we only care
        # about how the evaluator was *called*, not what it returns.

    # Patch the engine's preset->evaluator map for the duration of the
    # test. We swap "oversold" for the recorder while keeping the rest
    # of the production wiring intact.
    monkeypatch.setitem(engine_mod._EVALUATORS, "oversold", _recorder)

    bt = Backtester(fill_simulator=NoFillSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": feed},
        settings=BacktestSettings(
            initial_capital_quote=Decimal("1000"),
            risk_per_trade_pct=Decimal("0.01"),
            fee_pct=Decimal("0"),
        ),
    )
    assert report.bars_processed == len(feed)
    assert len(calls) == len(feed), (
        "evaluator must be called once per bar (no batched lookahead)"
    )
    for i, (n_candles, ind_id, ts) in enumerate(calls):
        # 1) WINDOW INVARIANT: at iteration i the evaluator sees i+1
        #    candles (bars 0..i inclusive). NEVER more.
        assert n_candles == i + 1, (
            f"LOOK-AHEAD LEAK at iteration {i}: evaluator saw "
            f"{n_candles} candles, expected {i + 1}"
        )
        # 2) INDICATOR INVARIANT: the snapshot is the i-th bar's own
        #    indicators object — not the next bar's.
        assert ind_id == id(expected_indicators[i]), (
            f"LOOK-AHEAD LEAK at iteration {i}: evaluator saw "
            f"a non-matching indicator snapshot"
        )
        # 3) TIMESTAMP INVARIANT: the latest candle timestamp matches
        #    bar i's timestamp (defensive; the above already implies it).
        assert ts == feed[i].candle.timestamp, (
            f"LOOK-AHEAD LEAK at iteration {i}: latest candle timestamp "
            f"is {ts}, expected {feed[i].candle.timestamp}"
        )
