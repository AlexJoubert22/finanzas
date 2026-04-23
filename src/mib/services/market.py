"""Market data orchestration service.

Routes a ticker to the right ``DataSource`` based on its shape, fans
out to quote + OHLCV + (optional) TradingView rating, and composes the
final ``SymbolResponse`` the router serialises.

The detection heuristic is documented on :func:`detect_ticker_kind`.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import pandas as pd

from mib.indicators.technical import compute_snapshot
from mib.logger import logger
from mib.models.market import Candle, Quote, SymbolResponse, TechnicalRating, TechnicalSnapshot
from mib.sources.ccxt_source import CCXTSource
from mib.sources.tradingview_ta import TradingViewTASource
from mib.sources.tv_exchange_map import is_forex_or_futures, resolve_tv_exchange
from mib.sources.yfinance_source import YFinanceSource

TickerKind = Literal["crypto", "stock"]

# Minimum OHLCV depth needed to compute every indicator in the spec set.
# EMA-200 requires 200 bars of close; we pad a bit to give the moving
# average a stable-state warm-up window.
_INDICATOR_WARMUP_BARS = 250

# Quote currencies we recognise as crypto-side when a separator is present.
# Per spec: USDT, USDC, BTC, ETH, EUR, USD.
_CRYPTO_QUOTES: frozenset[str] = frozenset(
    {"USDT", "USDC", "BTC", "ETH", "EUR", "USD"}
)

# Yahoo-style prefixes and suffixes that unambiguously mark a stock/ETF/fx/futures.
_YAHOO_PREFIXES: tuple[str, ...] = ("^",)  # ^GSPC, ^VIX, ^TNX
_YAHOO_SUFFIXES: tuple[str, ...] = ("=X", "=F")  # =X forex, =F futures


def detect_ticker_kind(ticker: str) -> TickerKind:
    """Heuristic: decide if a ticker belongs to crypto (CCXT) or stocks (yfinance).

    Rules applied in order:

    1. Starts with ``^``            → Yahoo index (``^GSPC``, ``^VIX`` …).
    2. Ends with ``=X`` or ``=F``   → Yahoo forex/futures (``EURUSD=X``, ``GC=F``).
    3. Contains ``/`` or ``-`` AND the right-hand side matches one of
       ``USDT / USDC / BTC / ETH / EUR / USD`` → crypto (``BTC/USDT``,
       ``ETH-USD``, ``SOL/BTC``).
    4. Contains ``/`` or ``-`` but with no recognisable crypto quote →
       Yahoo (covers ``BRK-B``, ``BF.B``, tickers with share-class suffix).
    5. Anything else (plain alphanumeric) → Yahoo (``AAPL``, ``SPY``,
       ``MSFT`` …).

    Args:
        ticker: Raw symbol as received from the URL path.

    Returns:
        ``"crypto"`` or ``"stock"``.
    """
    t = ticker.strip().upper()
    if t.startswith(_YAHOO_PREFIXES):
        return "stock"
    if t.endswith(_YAHOO_SUFFIXES):
        return "stock"
    if "/" in t or "-" in t:
        # Normalise separator so we can split uniformly.
        right = t.replace("/", "-").rsplit("-", 1)[-1]
        if right in _CRYPTO_QUOTES:
            return "crypto"
        return "stock"
    return "stock"


def normalise_crypto_symbol(ticker: str) -> str:
    """Convert ``BTC-USDT`` or ``btc/usdt`` into the canonical ``BTC/USDT``."""
    return ticker.strip().upper().replace("-", "/")


class MarketService:
    """Facade over the concrete data sources."""

    def __init__(
        self,
        ccxt_source: CCXTSource,
        yfinance_source: YFinanceSource,
        tv_source: TradingViewTASource | None = None,
    ) -> None:
        self._ccxt = ccxt_source
        self._yf = yfinance_source
        self._tv = tv_source

    async def get_symbol(
        self,
        raw_ticker: str,
        *,
        ohlcv_timeframe: str = "1h",
        ohlcv_limit: int = 100,
    ) -> SymbolResponse:
        """Fetch quote + OHLCV + indicators (+ optional TV rating).

        To compute EMA-200 reliably we always fetch at least
        ``_INDICATOR_WARMUP_BARS`` bars internally, regardless of what the
        caller asked for in ``ohlcv_limit``. Indicators are calculated on
        the long history; the ``candles`` returned in the response are
        truncated back to ``ohlcv_limit`` so the HTTP payload stays small.
        """
        kind = detect_ticker_kind(raw_ticker)
        fetch_limit = max(ohlcv_limit, _INDICATOR_WARMUP_BARS)

        if kind == "crypto":
            symbol = normalise_crypto_symbol(raw_ticker)
            quote, full_candles = await self._get_crypto(
                symbol, ohlcv_timeframe, fetch_limit
            )
            rating = await self._enrich_tv(symbol, kind, ohlcv_timeframe)
        else:
            quote, full_candles = await self._get_stock(
                raw_ticker.strip(), ohlcv_timeframe, fetch_limit
            )
            rating = await self._enrich_tv(raw_ticker.strip(), kind, ohlcv_timeframe)

        # Indicators computed on the full warm-up window (up to 250 bars).
        indicators = self._compute_indicators(full_candles)

        # Candles in the response are the recent-most slice requested by
        # the consumer — keeps the JSON payload small.
        candles = full_candles[-ohlcv_limit:] if full_candles else []

        return SymbolResponse(
            quote=quote,
            candles=candles,
            indicators=indicators,
            technical_rating=rating,
        )

    @staticmethod
    def _compute_indicators(candles: list[Candle]) -> TechnicalSnapshot | None:
        """Build a DataFrame from the response candles and compute the snapshot."""
        if len(candles) < 15:
            # RSI(14) needs ≥15 points; refuse early.
            return None
        try:
            df = pd.DataFrame(
                {
                    "open": [c.open for c in candles],
                    "high": [c.high for c in candles],
                    "low": [c.low for c in candles],
                    "close": [c.close for c in candles],
                    "volume": [c.volume for c in candles],
                }
            )
            return compute_snapshot(df)
        except Exception as exc:  # noqa: BLE001 - never fail the main response on TA
            logger.info("indicators compute soft-fail: {}", exc)
            return None

    # ─── Crypto path (CCXT) ────────────────────────────────────────────

    async def _get_crypto(
        self, symbol: str, timeframe: str, limit: int
    ) -> tuple[Quote, list[Candle]]:
        quote_task = asyncio.create_task(self._ccxt.fetch_quote(symbol))
        ohlcv_task = asyncio.create_task(
            self._ccxt.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        )
        quote, candles = await asyncio.gather(quote_task, ohlcv_task)
        return quote, candles

    # ─── Stock path (yfinance) ─────────────────────────────────────────

    async def _get_stock(
        self, ticker: str, timeframe: str, limit: int
    ) -> tuple[Quote, list[Candle]]:
        quote_task = asyncio.create_task(self._yf.fetch_quote(ticker))
        ohlcv_task = asyncio.create_task(
            self._yf.fetch_ohlcv(ticker, timeframe=timeframe, limit=limit)
        )
        quote, candles = await asyncio.gather(quote_task, ohlcv_task)
        return quote, candles

    # ─── TradingView enrichment (best-effort, bounded latency) ─────────

    async def _enrich_tv(
        self, symbol: str, kind: TickerKind, timeframe: str
    ) -> TechnicalRating | None:
        if self._tv is None:
            return None
        # TV doesn't understand Yahoo forex/futures suffixes; skip enrichment.
        if is_forex_or_futures(symbol):
            return None
        if kind == "crypto":
            tv_symbol = symbol.replace("/", "")
            exchange = "BINANCE"
        else:
            # Resolve the real exchange (NASDAQ / NYSE / AMEX / INDEX …).
            # Stocks not in the curated map fall back to NASDAQ with a
            # clean soft-fail on mismatch.
            exchange, tv_symbol = resolve_tv_exchange(symbol)
        try:
            return await self._tv.fetch_rating(
                tv_symbol, kind=kind, exchange=exchange, timeframe=timeframe
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must never block
            logger.info("tv enrichment soft-fail on {}: {}", symbol, exc)
            return None
