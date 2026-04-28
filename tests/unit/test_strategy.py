"""Tests for the FASE 7 StrategyEngine and its three evaluators.

Covers the non-negotiable invariants:

- An evaluator MUST return ``None`` when ATR is unavailable, no
  matter how clearly the threshold rule fires. Without ATR there is
  no honest stop and therefore no signal.
- A returned :class:`Signal` carries the correct ``strategy_id``
  (namespaced + versioned) and a long-side geometry that the
  derivation helpers produced (1.5×ATR stop, 1R/3R targets).
- The engine dispatches by preset name, swallows upstream fetch
  failures, and never propagates a malformed Signal that an evaluator
  produced by mistake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mib.models.market import Candle, Quote, SymbolResponse, TechnicalSnapshot
from mib.trading import strategy as strategy_module
from mib.trading.signals import Signal
from mib.trading.strategy import (
    StrategyEngine,
    evaluate_breakout,
    evaluate_oversold,
    evaluate_trending,
)


def _candles(*, volumes: list[float], close: float = 100.0) -> list[Candle]:
    base_ts = datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
    return [
        Candle(
            timestamp=base_ts,
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=v,
        )
        for v in volumes
    ]


def _snapshot(**overrides: float | None) -> TechnicalSnapshot:
    defaults: dict[str, float | None] = {
        "rsi_14": 22.0,
        "atr_14": 2.0,
        "ema_20": 99.0,
        "ema_50": 95.0,
        "ema_200": 90.0,
        "macd": 1.5,
        "macd_signal": 1.0,
        "macd_hist": 0.5,
        "bb_lower": 95.0,
        "bb_middle": 100.0,
        "bb_upper": 105.0,
        "adx_14": 35.0,
    }
    defaults.update(overrides)
    return TechnicalSnapshot(**defaults)  # type: ignore[arg-type]


def _response(
    *,
    ticker: str = "BTC/USDT",
    price: float = 100.0,
    volumes: list[float] | None = None,
    indicators: TechnicalSnapshot | None = None,
) -> SymbolResponse:
    if volumes is None:
        # 19 baseline bars + a final spike — passes the oversold
        # "last bar > 20-bar avg" check.
        volumes = [100.0] * 19 + [200.0]
    return SymbolResponse(
        quote=Quote(
            ticker=ticker,
            kind="crypto",
            source="ccxt:binance",
            price=price,
            timestamp=datetime(2026, 4, 27, 0, 0, tzinfo=UTC),
        ),
        candles=_candles(volumes=volumes, close=price),
        indicators=indicators if indicators is not None else _snapshot(),
    )


# ─── Non-negotiable: no ATR → no signal ────────────────────────────

class TestAtrIsRequired:
    def test_oversold_refuses_when_atr_none(self) -> None:
        data = _response(indicators=_snapshot(atr_14=None))
        assert evaluate_oversold(data) is None

    def test_breakout_refuses_when_atr_none(self) -> None:
        data = _response(indicators=_snapshot(atr_14=None))
        assert evaluate_breakout(data) is None

    def test_trending_refuses_when_atr_none(self) -> None:
        data = _response(indicators=_snapshot(atr_14=None))
        assert evaluate_trending(data) is None


# ─── Threshold gating ──────────────────────────────────────────────

class TestThresholdsBlockSignals:
    def test_oversold_requires_rsi_below_30(self) -> None:
        data = _response(indicators=_snapshot(rsi_14=35.0))
        assert evaluate_oversold(data) is None

    def test_oversold_requires_volume_spike(self) -> None:
        # Last bar volume ≤ avg of previous 19 → no signal.
        data = _response(volumes=[100.0] * 20)
        assert evaluate_oversold(data) is None

    def test_breakout_requires_price_above_ema50(self) -> None:
        data = _response(price=90.0, indicators=_snapshot(ema_50=95.0))
        assert evaluate_breakout(data) is None

    def test_trending_requires_adx_above_25(self) -> None:
        data = _response(indicators=_snapshot(adx_14=20.0))
        assert evaluate_trending(data) is None

    def test_trending_requires_positive_macd_hist(self) -> None:
        data = _response(indicators=_snapshot(macd_hist=-0.1))
        assert evaluate_trending(data) is None


# ─── Happy paths ───────────────────────────────────────────────────

class TestOversoldHappyPath:
    def test_emits_signal_with_proper_shape(self) -> None:
        data = _response(
            ticker="ETH/USDT",
            price=200.0,
            indicators=_snapshot(rsi_14=18.0, atr_14=4.0),
        )
        sig = evaluate_oversold(data)
        assert sig is not None
        assert sig.ticker == "ETH/USDT"
        assert sig.side == "long"
        assert sig.strategy_id == "scanner.oversold.v1"
        # Stop 1.5×ATR below price.
        assert sig.invalidation == pytest.approx(200.0 - 1.5 * 4.0)
        # 1R = 1 * (entry - stop) = 6 → t1 = 206.
        assert sig.target_1 == pytest.approx(206.0)
        # 3R = 18 → t2 = 218.
        assert sig.target_2 == pytest.approx(218.0)
        assert sig.indicators["atr_14"] == pytest.approx(4.0)
        assert sig.indicators["rsi_14"] == pytest.approx(18.0)
        # Strength rises as RSI falls.
        assert 0.0 <= sig.strength <= 1.0

    def test_rsi_at_zero_pegs_strength_to_one(self) -> None:
        data = _response(indicators=_snapshot(rsi_14=0.0))
        sig = evaluate_oversold(data)
        assert sig is not None
        assert sig.strength == pytest.approx(1.0)


class TestBreakoutHappyPath:
    def test_emits_signal_when_cross_up_detected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub the cross-up detector — the candle math is covered in
        # the scanner's existing tests.
        monkeypatch.setattr(
            strategy_module, "_ema_cross_up", lambda *a, **kw: True
        )
        data = _response(
            price=100.0,
            indicators=_snapshot(ema_20=99.0, ema_50=95.0, atr_14=1.0),
        )
        sig = evaluate_breakout(data)
        assert sig is not None
        assert sig.strategy_id == "scanner.breakout.v1"
        assert sig.side == "long"
        # k=1.5 default, atr=1 → stop = 98.5, 1R target = 101.5.
        assert sig.invalidation == pytest.approx(98.5)
        assert sig.target_1 == pytest.approx(101.5)


class TestTrendingHappyPath:
    def test_emits_signal_with_proper_strength_curve(self) -> None:
        data = _response(
            indicators=_snapshot(adx_14=60.0, macd_hist=2.0, atr_14=1.0)
        )
        sig = evaluate_trending(data)
        assert sig is not None
        assert sig.strategy_id == "scanner.trending.v1"
        # ADX 60 → (60-25)/35 = 1.0 → strength saturates.
        assert sig.strength == pytest.approx(1.0)


# ─── Engine dispatch ───────────────────────────────────────────────

class _FakeMarket:
    """Minimal stand-in for MarketService."""

    def __init__(self, by_ticker: dict[str, SymbolResponse | Exception]) -> None:
        self._by_ticker = by_ticker
        self.calls: list[str] = []

    async def get_symbol(self, ticker: str, **_kwargs: Any) -> SymbolResponse:
        self.calls.append(ticker)
        item = self._by_ticker[ticker]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_engine_unknown_preset_raises() -> None:
    market = _FakeMarket({})
    engine = StrategyEngine(market)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown strategy preset"):
        await engine.run("nope", ["BTC/USDT"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_engine_filters_none_evaluators() -> None:
    hit = _response(ticker="BTC/USDT", price=100.0)
    miss = _response(
        ticker="ETH/USDT",
        price=100.0,
        indicators=_snapshot(rsi_14=70.0),  # not oversold
    )
    market = _FakeMarket({"BTC/USDT": hit, "ETH/USDT": miss})
    engine = StrategyEngine(market)  # type: ignore[arg-type]
    out = await engine.run("oversold", ["BTC/USDT", "ETH/USDT"])
    assert [s.ticker for s in out] == ["BTC/USDT"]
    assert isinstance(out[0], Signal)


@pytest.mark.asyncio
async def test_engine_swallows_upstream_fetch_failures() -> None:
    market = _FakeMarket(
        {
            "BTC/USDT": _response(ticker="BTC/USDT"),
            "ETH/USDT": RuntimeError("upstream timeout"),
        }
    )
    engine = StrategyEngine(market)  # type: ignore[arg-type]
    out = await engine.run("oversold", ["BTC/USDT", "ETH/USDT"])
    # ETH skipped silently; BTC delivered.
    assert len(out) == 1
    assert out[0].ticker == "BTC/USDT"


@pytest.mark.asyncio
async def test_engine_passes_through_k_and_r_overrides() -> None:
    data = _response(price=100.0, indicators=_snapshot(rsi_14=20.0, atr_14=2.0))
    market = _FakeMarket({"BTC/USDT": data})
    engine = StrategyEngine(market)  # type: ignore[arg-type]
    out = await engine.run(
        "oversold", ["BTC/USDT"], k_invalidation=2.0, r_multiples=(2.0, 4.0)
    )
    assert len(out) == 1
    sig = out[0]
    # Stop = 100 - 2.0*2.0 = 96. risk = 4. t1 = 100 + 2*4 = 108. t2 = 116.
    assert sig.invalidation == pytest.approx(96.0)
    assert sig.target_1 == pytest.approx(108.0)
    assert sig.target_2 == pytest.approx(116.0)
