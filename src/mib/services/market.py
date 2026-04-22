"""Market data orchestration service.

Routes a ticker to the right ``DataSource`` based on its shape, fans
out to quote + OHLCV + (optional) TradingView rating, and composes the
final ``SymbolResponse`` the router serialises.

The detection heuristic is documented on :func:`detect_ticker_kind`.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from mib.logger import logger
from mib.models.market import Candle, Quote, SymbolResponse, TechnicalRating
from mib.sources.ccxt_source import CCXTSource
from mib.sources.tradingview_ta import TradingViewTASource
from mib.sources.yfinance_source import YFinanceSource

TickerKind = Literal["crypto", "stock"]

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
        """Fetch quote + OHLCV (+ optional TV rating) for ``raw_ticker``."""
        kind = detect_ticker_kind(raw_ticker)
        if kind == "crypto":
            symbol = normalise_crypto_symbol(raw_ticker)
            quote, candles = await self._get_crypto(symbol, ohlcv_timeframe, ohlcv_limit)
            rating = await self._enrich_tv(symbol, kind, ohlcv_timeframe)
        else:
            quote, candles = await self._get_stock(
                raw_ticker.strip(), ohlcv_timeframe, ohlcv_limit
            )
            rating = await self._enrich_tv(raw_ticker.strip(), kind, ohlcv_timeframe)
        return SymbolResponse(quote=quote, candles=candles, technical_rating=rating)

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
        # TV expects `BTCUSDT` for crypto (no slash) and `AAPL` as-is for stocks.
        if kind == "crypto":
            tv_symbol = symbol.replace("/", "")
            exchange = "BINANCE"
        else:
            tv_symbol = symbol
            exchange = "NASDAQ"  # best-effort; TV tolerates mismatches with a retry
        try:
            return await self._tv.fetch_rating(
                tv_symbol, kind=kind, exchange=exchange, timeframe=timeframe
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must never block
            logger.info("tv enrichment soft-fail on {}: {}", symbol, exc)
            return None
