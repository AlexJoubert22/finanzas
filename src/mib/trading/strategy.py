"""Strategy engine — turns scanner-style threshold rules into full
:class:`Signal` objects with ATR-derived stops and R-multiple targets.

Three evaluators are wired today, one per existing scanner preset:

- ``scanner.oversold.v1``  — RSI(14) < 30 on 1h + volume > 20-bar avg
- ``scanner.breakout.v1``  — price > EMA50 AND EMA20 cross-up over EMA50 on 4h
- ``scanner.trending.v1``  — ADX(14) > 25 AND MACD hist > 0 on 1d

Why a separate module rather than reusing :class:`ScannerService`:

The scanner produces dicts for the ``/scan`` Telegram + HTTP endpoint
— a human-facing summary. Strategies produce :class:`Signal` objects
with mandatory stop and targets — a thesis ready for the executor.
Different return shapes, different concerns. They share the same
threshold logic on purpose: when the strategy is updated to ``v2``
that change is local to this module and does not silently alter the
``/scan`` UI behaviour.

The engine is **pure**: it returns ``list[Signal]`` and never touches
the DB. Persistence is layered on top by a scheduler job (added in
7.5). This keeps ``/scan`` from polluting the signals table on
interactive calls and lets the FASE 12 backtester replay history
through the same engine without poisoning live data.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from typing import Final, Protocol

import pandas as pd
import pandas_ta as pta

from mib.logger import logger
from mib.models.market import SymbolResponse
from mib.services.market import MarketService
from mib.services.scanner import PresetName, _preset_timeframe
from mib.trading.signals import (
    Signal,
    derive_invalidation_long,
    derive_targets,
)

# All three current presets imply a long bias (oversold reverts up,
# breakout-from-EMA50 trades up, ADX+positive-MACD-hist trades up).
# Short-side strategies are FASE 8 territory.
_DEFAULT_K_INVALIDATION: Final[float] = 1.5
_DEFAULT_R_MULTIPLES: Final[tuple[float, float]] = (1.0, 3.0)
_INDICATOR_WARMUP_BARS: Final[int] = 250


# ─── Strategy evaluators (pure functions) ──────────────────────────

def evaluate_oversold(
    data: SymbolResponse,
    *,
    k_invalidation: float = _DEFAULT_K_INVALIDATION,
    r_multiples: Sequence[float] = _DEFAULT_R_MULTIPLES,
) -> Signal | None:
    """Long signal if RSI(14) < 30 and last bar volume > 20-bar avg."""
    ind = data.indicators
    if ind is None or ind.rsi_14 is None or ind.rsi_14 >= 30.0:
        return None
    if ind.atr_14 is None:
        # Non-negotiable rule from the FASE 7 spec: no ATR → no stop → no signal.
        return None

    vol_tail = [c.volume for c in data.candles[-20:] if c.volume is not None]
    if len(vol_tail) < 20:
        return None
    avg_vol = statistics.mean(vol_tail[:-1])
    if avg_vol <= 0 or vol_tail[-1] <= avg_vol:
        return None

    entry = data.quote.price
    invalidation = derive_invalidation_long(entry, ind.atr_14, k=k_invalidation)
    t1, t2 = derive_targets(entry, invalidation, side="long", r_multiples=r_multiples)
    strength = _normalise_strength((30.0 - ind.rsi_14) / 30.0)

    return Signal(
        ticker=data.quote.ticker,
        side="long",
        strength=strength,
        timeframe=_preset_timeframe("oversold"),
        entry_zone=(entry, entry),
        invalidation=invalidation,
        target_1=t1,
        target_2=t2,
        rationale=(
            f"RSI={ind.rsi_14:.1f} (<30), vol/avg20={vol_tail[-1] / avg_vol:.1f}x"
        ),
        indicators={"rsi_14": ind.rsi_14, "atr_14": ind.atr_14},
        strategy_id="scanner.oversold.v1",
    )


def evaluate_breakout(
    data: SymbolResponse,
    *,
    k_invalidation: float = _DEFAULT_K_INVALIDATION,
    r_multiples: Sequence[float] = _DEFAULT_R_MULTIPLES,
) -> Signal | None:
    """Long signal if price > EMA50 AND EMA20 just crossed up over EMA50."""
    ind = data.indicators
    if ind is None or ind.ema_20 is None or ind.ema_50 is None:
        return None
    if ind.atr_14 is None:
        return None
    entry = data.quote.price
    if entry <= ind.ema_50:
        return None
    if not _ema_cross_up(data, fast=20, slow=50, lookback=5):
        return None

    invalidation = derive_invalidation_long(entry, ind.atr_14, k=k_invalidation)
    t1, t2 = derive_targets(entry, invalidation, side="long", r_multiples=r_multiples)
    # Strength: how far price sits above EMA50, normalised by ATR.
    distance_in_atr = (entry - ind.ema_50) / ind.atr_14
    strength = _normalise_strength(min(distance_in_atr / 3.0, 1.0))

    return Signal(
        ticker=data.quote.ticker,
        side="long",
        strength=strength,
        timeframe=_preset_timeframe("breakout"),
        entry_zone=(entry, entry),
        invalidation=invalidation,
        target_1=t1,
        target_2=t2,
        rationale=(
            f"price {entry:.4g} > EMA50 {ind.ema_50:.4g}; "
            f"EMA20 {ind.ema_20:.4g} crossed up EMA50"
        ),
        indicators={
            "ema_20": ind.ema_20,
            "ema_50": ind.ema_50,
            "atr_14": ind.atr_14,
        },
        strategy_id="scanner.breakout.v1",
    )


def evaluate_trending(
    data: SymbolResponse,
    *,
    k_invalidation: float = _DEFAULT_K_INVALIDATION,
    r_multiples: Sequence[float] = _DEFAULT_R_MULTIPLES,
) -> Signal | None:
    """Long signal if ADX(14) > 25 AND MACD histogram > 0."""
    ind = data.indicators
    if ind is None or ind.adx_14 is None or ind.macd_hist is None:
        return None
    if ind.adx_14 <= 25.0 or ind.macd_hist <= 0.0:
        return None
    if ind.atr_14 is None:
        return None

    entry = data.quote.price
    invalidation = derive_invalidation_long(entry, ind.atr_14, k=k_invalidation)
    t1, t2 = derive_targets(entry, invalidation, side="long", r_multiples=r_multiples)
    # Strength scales linearly with ADX above 25; saturates around 60.
    strength = _normalise_strength((ind.adx_14 - 25.0) / 35.0)

    return Signal(
        ticker=data.quote.ticker,
        side="long",
        strength=strength,
        timeframe=_preset_timeframe("trending"),
        entry_zone=(entry, entry),
        invalidation=invalidation,
        target_1=t1,
        target_2=t2,
        rationale=(
            f"ADX={ind.adx_14:.1f} (>25), MACD hist={ind.macd_hist:+.4g} (>0)"
        ),
        indicators={
            "adx_14": ind.adx_14,
            "macd_hist": ind.macd_hist,
            "atr_14": ind.atr_14,
        },
        strategy_id="scanner.trending.v1",
    )


# ─── Engine ────────────────────────────────────────────────────────

class _Evaluator(Protocol):
    """Common shape for every preset evaluator."""

    def __call__(
        self,
        data: SymbolResponse,
        *,
        k_invalidation: float = ...,
        r_multiples: Sequence[float] = ...,
    ) -> Signal | None: ...


class StrategyEngine:
    """Fan-out per ticker, evaluate, return ``list[Signal]``.

    Bounded concurrency mirrors :class:`ScannerService` so we don't
    fan out 20 simultaneous requests to upstream APIs.
    """

    _PRESETS: Final[dict[PresetName, _Evaluator]] = {
        "oversold": evaluate_oversold,
        "breakout": evaluate_breakout,
        "trending": evaluate_trending,
    }

    def __init__(self, market: MarketService, *, max_concurrency: int = 4) -> None:
        self._market = market
        self._sem = asyncio.Semaphore(max_concurrency)

    async def run(
        self,
        preset: PresetName,
        tickers: list[str],
        *,
        k_invalidation: float = _DEFAULT_K_INVALIDATION,
        r_multiples: Sequence[float] = _DEFAULT_R_MULTIPLES,
    ) -> list[Signal]:
        evaluator = self._PRESETS.get(preset)
        if evaluator is None:
            raise ValueError(f"unknown strategy preset: {preset}")

        async def _one(t: str) -> Signal | None:
            async with self._sem:
                try:
                    data = await self._market.get_symbol(
                        t,
                        ohlcv_timeframe=_preset_timeframe(preset),
                        ohlcv_limit=_INDICATOR_WARMUP_BARS,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info("strategy: skip {} ({}): {}", preset, t, exc)
                    return None
            try:
                return evaluator(
                    data,
                    k_invalidation=k_invalidation,
                    r_multiples=r_multiples,
                )
            except ValueError as exc:
                # A strategy that emits a malformed Signal (geometry
                # contradicting the side claimed) is a bug; surface it
                # clearly without killing the whole batch.
                logger.warning(
                    "strategy: {} produced invalid signal for {}: {}",
                    preset,
                    t,
                    exc,
                )
                return None

        out = await asyncio.gather(*(_one(t) for t in tickers))
        return [s for s in out if s is not None]


# ─── Helpers ───────────────────────────────────────────────────────

def _ema_cross_up(
    data: SymbolResponse, *, fast: int, slow: int, lookback: int
) -> bool:
    """True iff fast EMA crosses above slow EMA in the last ``lookback`` bars.

    Mirrors :meth:`ScannerService._ema_cross_up` so both modules
    stay aligned on what "cross up" means.
    """
    closes = pd.Series([c.close for c in data.candles])
    if len(closes) < slow + lookback:
        return False
    e_fast = pta.ema(closes, length=fast)
    e_slow = pta.ema(closes, length=slow)
    if e_fast is None or e_slow is None:
        return False
    if e_fast.iloc[-1] <= e_slow.iloc[-1]:
        return False
    try:
        return bool(e_fast.iloc[-lookback] < e_slow.iloc[-lookback])
    except IndexError:
        return False


def _normalise_strength(raw: float) -> float:
    """Clamp ``raw`` to the [0, 1] band that :class:`Signal` expects."""
    if raw < 0.0:
        return 0.0
    if raw > 1.0:
        return 1.0
    return float(raw)
