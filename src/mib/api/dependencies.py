"""FastAPI dependencies: long-lived singletons shared across requests.

DataSource objects hold HTTP pools and exchange clients, so we create
them once at startup and reuse them.
"""

from __future__ import annotations

from mib.services.market import MarketService
from mib.sources.ccxt_source import CCXTSource
from mib.sources.tradingview_ta import TradingViewTASource
from mib.sources.yfinance_source import YFinanceSource

_ccxt: CCXTSource | None = None
_yf: YFinanceSource | None = None
_tv: TradingViewTASource | None = None
_market: MarketService | None = None


def get_ccxt_source() -> CCXTSource:
    global _ccxt  # noqa: PLW0603 - intentional module-level singleton
    if _ccxt is None:
        _ccxt = CCXTSource(exchange_id="binance")
    return _ccxt


def get_yfinance_source() -> YFinanceSource:
    global _yf  # noqa: PLW0603
    if _yf is None:
        _yf = YFinanceSource()
    return _yf


def get_tradingview_source() -> TradingViewTASource:
    global _tv  # noqa: PLW0603
    if _tv is None:
        _tv = TradingViewTASource()
    return _tv


def get_market_service() -> MarketService:
    global _market  # noqa: PLW0603
    if _market is None:
        _market = MarketService(
            ccxt_source=get_ccxt_source(),
            yfinance_source=get_yfinance_source(),
            tv_source=get_tradingview_source(),
        )
    return _market


async def shutdown_sources() -> None:
    """Close long-lived connections (called from FastAPI lifespan)."""
    if _ccxt is not None:
        await _ccxt.close()
