"""TradingView technical-analysis source.

Strictly an ENRICHMENT: any failure is swallowed at the service layer
and the symbol response is returned without a ``technical_rating``
field. Hard timeout is 3 s to avoid blocking the main response.

Uses the ``tradingview_ta`` package which scrapes the unofficial JSON
endpoint TradingView exposes on its widget. No API key.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from tradingview_ta import (  # type: ignore[import-untyped]
    Interval,
    TA_Handler,
)

from mib.logger import logger
from mib.models.market import TechnicalRating
from mib.sources.base import DataSource, RateLimiter, SourceError

_TTL_RATING_SEC = 300  # 5 min per spec §4
_HARD_TIMEOUT_SEC = 3.0  # spec: enrichment opcional — cortar a 3 s.


# Map our timeframe codes to tradingview_ta's Interval enum.
_TF_TO_TV: dict[str, str] = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1wk": Interval.INTERVAL_1_WEEK,
}


class TradingViewTASource(DataSource):
    """Optional enrichment: fetches TradingView's aggregate recommendation."""

    name: ClassVar[str] = "tradingview_ta"
    # Don't waste retries on enrichment — fail fast.
    max_retries: ClassVar[int] = 1

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=20, period_seconds=60.0))

    async def fetch_rating(
        self,
        ticker: str,
        *,
        kind: str,
        exchange: str = "BINANCE",
        timeframe: str = "1h",
    ) -> TechnicalRating | None:
        """Return the TV rating or None if the provider doesn't respond in time.

        Args:
            ticker: Symbol as TV expects it (``BTCUSDT`` for crypto, ``AAPL``
                for stocks — no slashes). Callers are responsible for
                translating.
            kind: ``"crypto"`` or ``"stock"``; maps to TV's ``screener``.
            exchange: Exchange code (``BINANCE``, ``NASDAQ``, ``NYSE``…).
            timeframe: One of the keys of ``_TF_TO_TV``.
        """
        try:
            return await asyncio.wait_for(
                self._fetch_rating_inner(ticker, kind, exchange, timeframe),
                timeout=_HARD_TIMEOUT_SEC,
            )
        except TimeoutError:
            logger.info(
                "tradingview_ta: 3s timeout on {}/{}/{} — skipping enrichment",
                exchange,
                ticker,
                timeframe,
            )
            return None
        except SourceError as exc:
            logger.info("tradingview_ta: source error on {} — skipping: {}", ticker, exc)
            return None

    async def _fetch_rating_inner(
        self,
        ticker: str,
        kind: str,
        exchange: str,
        timeframe: str,
    ) -> TechnicalRating | None:
        key = f"tv:{exchange}:{ticker}:{timeframe}"
        screener = "crypto" if kind == "crypto" else "america"
        interval = _TF_TO_TV.get(timeframe, Interval.INTERVAL_1_HOUR)

        async def loader() -> dict[str, int | str]:
            def _sync_call() -> dict[str, int | str]:
                handler = TA_Handler(
                    symbol=ticker,
                    exchange=exchange,
                    screener=screener,
                    interval=interval,
                )
                analysis = handler.get_analysis()
                summary = analysis.summary
                return {
                    "recommendation": str(summary.get("RECOMMENDATION", "NEUTRAL")),
                    "buy": int(summary.get("BUY", 0)),
                    "sell": int(summary.get("SELL", 0)),
                    "neutral": int(summary.get("NEUTRAL", 0)),
                }

            return await asyncio.to_thread(_sync_call)

        raw = await self._cached_call(
            cache_key=key,
            ttl_seconds=_TTL_RATING_SEC,
            endpoint=f"get_analysis:{exchange}:{ticker}:{timeframe}",
            loader=loader,
        )
        return TechnicalRating(
            recommendation=str(raw["recommendation"]),
            buy=int(raw["buy"]),
            sell=int(raw["sell"]),
            neutral=int(raw["neutral"]),
            timeframe=timeframe,
        )

    async def health(self) -> bool:
        """Enrichment — we never gate the app on TV's liveness."""
        return True
