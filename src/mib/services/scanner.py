"""Multi-ticker scanner with rule-based presets.

Presets (spec §4):

- ``oversold``  — RSI(14) < 30 on 1h + volume > 20-bar average
- ``breakout``  — price > EMA50 AND EMA20 crossing up over EMA50 on 4h
- ``trending``  — ADX(14) > 25 AND MACD histogram positive on 1d

Inputs: a list of tickers to scan (crypto symbols like ``BTC/USDT`` and
stocks both work). Each scan ships through ``MarketService.get_symbol``
so we reuse the cache and the fetch-warmup logic; there's no
duplication of data-source plumbing.

Output: a list of dicts with the winning tickers plus the specific
indicator values that qualified them. Optionally wrapped with an IA
summary paragraph (AIService.scan_summary).
"""

from __future__ import annotations

import asyncio
import statistics
from typing import Any, Literal

import yaml

from mib.logger import logger
from mib.models.market import SymbolResponse
from mib.services.market import MarketService

PresetName = Literal["oversold", "breakout", "trending"]


class ScannerService:
    def __init__(self, market: MarketService) -> None:
        self._market = market
        self._presets = {
            "oversold": self._eval_oversold,
            "breakout": self._eval_breakout,
            "trending": self._eval_trending,
        }

    async def run(
        self,
        preset: PresetName,
        tickers: list[str],
    ) -> list[dict[str, Any]]:
        """Evaluate ``preset`` against every ticker and return hits.

        Any ticker whose data cannot be fetched is skipped (logged at
        INFO, never raised). Hits are ordered by the preset-specific
        score (e.g. lowest RSI first for oversold).
        """
        evaluator = self._presets.get(preset)
        if evaluator is None:
            raise ValueError(f"unknown scanner preset: {preset}")

        # Use a bounded concurrency so we don't fan out 20 parallel
        # requests against upstream APIs.
        sem = asyncio.Semaphore(4)

        async def _one(t: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    data = await self._market.get_symbol(
                        t, ohlcv_timeframe=_preset_timeframe(preset), ohlcv_limit=250
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info("scanner: skip {} ({}): {}", preset, t, exc)
                    return None
            return evaluator(t, data)

        raw = await asyncio.gather(*(_one(t) for t in tickers))
        hits: list[dict[str, Any]] = [r for r in raw if r is not None]
        return _sort_hits(hits, preset)

    # ─── Preset evaluators ─────────────────────────────────────────────

    def _eval_oversold(self, ticker: str, d: SymbolResponse) -> dict[str, Any] | None:
        ind = d.indicators
        if ind is None or ind.rsi_14 is None or ind.rsi_14 >= 30.0:
            return None
        # Volume > 20-bar average.
        vol_tail = [c.volume for c in d.candles[-20:] if c.volume is not None]
        if len(vol_tail) < 20:
            return None
        avg_vol = statistics.mean(vol_tail[:-1])
        if avg_vol <= 0 or vol_tail[-1] <= avg_vol:
            return None
        return {
            "ticker": ticker,
            "reason": f"RSI={ind.rsi_14:.1f} (<30) · vol/avg20={vol_tail[-1] / avg_vol:.1f}×",
            "rsi": ind.rsi_14,
            "price": d.quote.price,
        }

    def _eval_breakout(self, ticker: str, d: SymbolResponse) -> dict[str, Any] | None:
        ind = d.indicators
        if (
            ind is None
            or ind.ema_20 is None
            or ind.ema_50 is None
            or d.quote.price is None
        ):
            return None
        if d.quote.price <= ind.ema_50:
            return None
        # Approximation of "EMA20 crossing up EMA50": EMA20 is above EMA50
        # AND was below it N=5 bars ago. Because we only expose the last
        # snapshot, we use the series via a quick pandas rebuild.
        cross_up = self._ema_cross_up(d, fast=20, slow=50, lookback=5)
        if not cross_up:
            return None
        return {
            "ticker": ticker,
            "reason": (
                f"price {d.quote.price:.2f} > EMA50 {ind.ema_50:.2f} · "
                f"EMA20 {ind.ema_20:.2f} cruzó arriba EMA50"
            ),
            "price": d.quote.price,
            "ema_20": ind.ema_20,
            "ema_50": ind.ema_50,
        }

    def _eval_trending(self, ticker: str, d: SymbolResponse) -> dict[str, Any] | None:
        ind = d.indicators
        if (
            ind is None
            or ind.adx_14 is None
            or ind.macd_hist is None
            or ind.adx_14 <= 25.0
            or ind.macd_hist <= 0.0
        ):
            return None
        return {
            "ticker": ticker,
            "reason": f"ADX={ind.adx_14:.1f} (>25) · MACD hist={ind.macd_hist:+.2f} (>0)",
            "adx": ind.adx_14,
            "macd_hist": ind.macd_hist,
            "price": d.quote.price,
        }

    # ─── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _ema_cross_up(
        d: SymbolResponse, *, fast: int, slow: int, lookback: int
    ) -> bool:
        """True if the fast EMA crosses above the slow EMA in the last ``lookback`` bars."""
        # Rebuild the short series we need on the fly; cheap given ≤250 bars.
        import pandas as pd
        import pandas_ta as pta

        closes = pd.Series([c.close for c in d.candles])
        if len(closes) < slow + lookback:
            return False
        e_fast = pta.ema(closes, length=fast)
        e_slow = pta.ema(closes, length=slow)
        if e_fast is None or e_slow is None:
            return False
        if e_fast.iloc[-1] <= e_slow.iloc[-1]:
            return False
        # It had to be below `lookback` bars ago.
        try:
            return bool(e_fast.iloc[-lookback] < e_slow.iloc[-lookback])
        except IndexError:
            return False


def _preset_timeframe(preset: PresetName) -> str:
    return {"oversold": "1h", "breakout": "4h", "trending": "1d"}.get(preset, "1h")


def _sort_hits(hits: list[dict[str, Any]], preset: PresetName) -> list[dict[str, Any]]:
    if preset == "oversold":
        return sorted(hits, key=lambda h: h.get("rsi", 100.0))
    if preset == "trending":
        return sorted(hits, key=lambda h: -h.get("adx", 0.0))
    return hits


def load_scanner_presets(path: str = "config/scanner_presets.yaml") -> dict[str, Any]:
    """Best-effort YAML loader for the preset definitions used by /scan."""
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
