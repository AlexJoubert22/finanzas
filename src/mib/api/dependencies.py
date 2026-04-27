"""FastAPI dependencies: long-lived singletons shared across requests.

DataSource objects hold HTTP pools and exchange clients, so we create
them once at startup and reuse them.
"""

from __future__ import annotations

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider
from mib.ai.providers.gemini_provider import GeminiProvider
from mib.ai.providers.groq_provider import GroqProvider
from mib.ai.providers.openrouter_provider import OpenRouterProvider
from mib.ai.router import AIRouter
from mib.services.ai_service import AIService
from mib.services.macro import MacroService
from mib.services.market import MarketService
from mib.services.news import NewsService
from mib.services.scanner import ScannerService
from mib.sources.alphavantage import AlphaVantageSource
from mib.sources.ccxt_reader import CCXTReader
from mib.sources.coingecko import CoinGeckoSource
from mib.sources.finnhub import FinnhubSource
from mib.sources.fred import FREDSource
from mib.sources.rss import RSSSource
from mib.sources.tradingview_ta import TradingViewTASource
from mib.sources.yfinance_source import YFinanceSource
from mib.trading.strategy import StrategyEngine

_ccxt: CCXTReader | None = None
_yf: YFinanceSource | None = None
_tv: TradingViewTASource | None = None
_cg: CoinGeckoSource | None = None
_av: AlphaVantageSource | None = None
_finnhub: FinnhubSource | None = None
_fred: FREDSource | None = None
_rss: RSSSource | None = None

_market: MarketService | None = None
_macro: MacroService | None = None
_news: NewsService | None = None

_ai_router: AIRouter | None = None
_ai_service: AIService | None = None
_scanner: ScannerService | None = None
_strategy_engine: StrategyEngine | None = None


# ─── Source singletons ────────────────────────────────────────────────

def get_ccxt_source() -> CCXTReader:
    global _ccxt  # noqa: PLW0603
    if _ccxt is None:
        _ccxt = CCXTReader(exchange_id="binance")
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


def get_coingecko_source() -> CoinGeckoSource:
    global _cg  # noqa: PLW0603
    if _cg is None:
        _cg = CoinGeckoSource()
    return _cg


def get_alphavantage_source() -> AlphaVantageSource:
    global _av  # noqa: PLW0603
    if _av is None:
        _av = AlphaVantageSource()
    return _av


def get_finnhub_source() -> FinnhubSource:
    global _finnhub  # noqa: PLW0603
    if _finnhub is None:
        _finnhub = FinnhubSource()
    return _finnhub


def get_fred_source() -> FREDSource:
    global _fred  # noqa: PLW0603
    if _fred is None:
        _fred = FREDSource()
    return _fred


def get_rss_source() -> RSSSource:
    global _rss  # noqa: PLW0603
    if _rss is None:
        _rss = RSSSource()
    return _rss


# ─── Service singletons ───────────────────────────────────────────────

def get_market_service() -> MarketService:
    global _market  # noqa: PLW0603
    if _market is None:
        _market = MarketService(
            ccxt_source=get_ccxt_source(),
            yfinance_source=get_yfinance_source(),
            tv_source=get_tradingview_source(),
        )
    return _market


def get_macro_service() -> MacroService:
    global _macro  # noqa: PLW0603
    if _macro is None:
        _macro = MacroService(
            yf=get_yfinance_source(),
            fred=get_fred_source(),
            cg=get_coingecko_source(),
        )
    return _macro


def get_news_service() -> NewsService:
    global _news  # noqa: PLW0603
    if _news is None:
        _news = NewsService(finnhub=get_finnhub_source(), rss=get_rss_source())
    return _news


# ─── IA wiring ────────────────────────────────────────────────────────

def get_ai_router() -> AIRouter:
    global _ai_router  # noqa: PLW0603
    if _ai_router is None:
        providers: dict[ProviderId, AIProvider] = {
            ProviderId.GROQ: GroqProvider(),
            ProviderId.OPENROUTER: OpenRouterProvider(),
            ProviderId.GEMINI: GeminiProvider(),
        }
        _ai_router = AIRouter(providers=providers)
    return _ai_router


def get_ai_service() -> AIService:
    global _ai_service  # noqa: PLW0603
    if _ai_service is None:
        _ai_service = AIService(router=get_ai_router())
    return _ai_service


def get_scanner_service() -> ScannerService:
    global _scanner  # noqa: PLW0603
    if _scanner is None:
        _scanner = ScannerService(market=get_market_service())
    return _scanner


def get_strategy_engine() -> StrategyEngine:
    """FASE 7+ trading entrypoint — produces ``list[Signal]`` from presets."""
    global _strategy_engine  # noqa: PLW0603
    if _strategy_engine is None:
        _strategy_engine = StrategyEngine(market=get_market_service())
    return _strategy_engine


async def shutdown_sources() -> None:
    """Close long-lived connections (called from FastAPI lifespan)."""
    if _ccxt is not None:
        await _ccxt.close()
