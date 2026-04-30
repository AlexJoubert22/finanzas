"""FastAPI dependencies: long-lived singletons shared across requests.

DataSource objects hold HTTP pools and exchange clients, so we create
them once at startup and reuse them.
"""

from __future__ import annotations

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider
from mib.ai.providers.gemini_provider import GeminiProvider
from mib.ai.providers.groq_provider import GroqProvider
from mib.ai.providers.nvidia_provider import NvidiaProvider
from mib.ai.providers.openrouter_provider import OpenRouterProvider
from mib.ai.router import AIRouter
from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.services.ai_service import AIService
from mib.services.macro import MacroService
from mib.services.market import MarketService
from mib.services.news import NewsService
from mib.services.scanner import ScannerService
from mib.sources.alphavantage import AlphaVantageSource
from mib.sources.ccxt_reader import CCXTReader
from mib.sources.ccxt_trader import CCXTTrader
from mib.sources.coingecko import CoinGeckoSource
from mib.sources.finnhub import FinnhubSource
from mib.sources.fred import FREDSource
from mib.sources.rss import RSSSource
from mib.sources.tradingview_ta import TradingViewTASource
from mib.sources.yfinance_source import YFinanceSource
from mib.trading.alerter import NullAlerter, TelegramAlerter, TelegramBotAlerter
from mib.trading.executor import OrderExecutor
from mib.trading.fill_detector import FillDetector
from mib.trading.mode_service import ModeService
from mib.trading.order_repo import OrderRepository
from mib.trading.portfolio import PortfolioState
from mib.trading.reconcile import Reconciler
from mib.trading.risk.correlation_groups import CorrelationGroups
from mib.trading.risk.gates.correlation_group import CorrelationGroupGate
from mib.trading.risk.gates.daily_drawdown import DailyDrawdownGate
from mib.trading.risk.gates.exposure_ticker import ExposurePerTickerGate
from mib.trading.risk.gates.kill_switch import KillSwitchGate
from mib.trading.risk.gates.max_concurrent import MaxConcurrentTradesGate
from mib.trading.risk.gates.signals_rate_limit import SignalsPerHourRateLimitGate
from mib.trading.risk.manager import RiskManager
from mib.trading.risk.protocol import Gate
from mib.trading.risk.repo import RiskDecisionRepository
from mib.trading.risk.state import TradingStateService
from mib.trading.signal_repo import SignalRepository
from mib.trading.sizing import PositionSizer
from mib.trading.stop_placer import NativeStopPlacer
from mib.trading.strategy import StrategyEngine
from mib.trading.trade_repo import TradeRepository

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
_signal_repo: SignalRepository | None = None
_ccxt_trader: CCXTTrader | None = None
_portfolio_state: PortfolioState | None = None
_trading_state_service: TradingStateService | None = None
_risk_decision_repo: RiskDecisionRepository | None = None
_risk_manager: RiskManager | None = None
_order_repo: OrderRepository | None = None
_trade_repo: TradeRepository | None = None
_reconciler: Reconciler | None = None
_executor: OrderExecutor | None = None
_mode_service: ModeService | None = None


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
            ProviderId.NVIDIA: NvidiaProvider(),
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


def get_signal_repository() -> SignalRepository:
    """FASE 7+ persistence boundary for the ``signals`` table."""
    global _signal_repo  # noqa: PLW0603
    if _signal_repo is None:
        _signal_repo = SignalRepository(async_session_factory)
    return _signal_repo


def get_order_repository() -> OrderRepository:
    """FASE 9.2+ persistence boundary for the ``orders`` table."""
    global _order_repo  # noqa: PLW0603
    if _order_repo is None:
        _order_repo = OrderRepository(async_session_factory)
    return _order_repo


def get_ccxt_trader() -> CCXTTrader:
    """FASE 9.1+ executor singleton, wired to Binance Testnet.

    Reads sandbox credentials from settings. ``dry_run`` defaults to
    ``not trading_enabled`` so writes stay gated until the operator
    explicitly flips the master switch (FASE 14). The triple seatbelt
    inside ``CCXTTrader`` adds a third ``is_sandbox`` check that hard-
    blocks any production endpoint until that day.
    """
    global _ccxt_trader  # noqa: PLW0603
    if _ccxt_trader is None:
        s = get_settings()
        _ccxt_trader = CCXTTrader(
            exchange_id="binance",
            api_key=s.binance_sandbox_api_key,
            api_secret=s.binance_sandbox_secret,
            base_url=s.binance_sandbox_base_url,
            dry_run=not s.trading_enabled,
            order_repo=get_order_repository(),
        )
    return _ccxt_trader


def get_mode_service() -> ModeService:
    """FASE 10+ trading mode reader/transitioner.

    Backed by the same ``trading_state`` singleton. The
    ``ModeTransitionRepository`` (audit log) is wired in FASE 10.2;
    until then the service still works (cache update only).
    """
    global _mode_service  # noqa: PLW0603
    if _mode_service is None:
        _mode_service = ModeService(
            session_factory=async_session_factory,
            state_service=get_trading_state_service(),
        )
    return _mode_service


def get_trade_repository() -> TradeRepository:
    """FASE 9.4+ persistence boundary for the ``trades`` table."""
    global _trade_repo  # noqa: PLW0603
    if _trade_repo is None:
        _trade_repo = TradeRepository(async_session_factory)
    return _trade_repo


def get_alerter() -> TelegramAlerter:
    """Best-effort Telegram alerter wired to the running bot if any.

    Returns a :class:`NullAlerter` when the bot isn't running so the
    executor / reconciler still work in API-only mode.
    """
    from mib.telegram.bot import get_bot_app  # noqa: PLC0415

    bot_app = get_bot_app()
    if bot_app is None:
        return NullAlerter()
    return TelegramBotAlerter(bot_app)


def get_order_executor() -> OrderExecutor:
    """FASE 9.6+ end-to-end executor singleton.

    Lazily resolves the alerter at first call so the bot has had time
    to start up; subsequent calls reuse the same instance.
    """
    global _executor  # noqa: PLW0603
    if _executor is None:
        trader = get_ccxt_trader()
        order_repo = get_order_repository()
        alerter = get_alerter()
        _executor = OrderExecutor(
            trader=trader,
            order_repo=order_repo,
            trade_repo=get_trade_repository(),
            fill_detector=FillDetector(trader, order_repo),
            stop_placer=NativeStopPlacer(trader, order_repo, alerter),
            alerter=alerter,
            exchange_id=(
                "binance_sandbox" if trader.is_sandbox else "binance"
            ),
        )
    return _executor


def get_reconciler() -> Reconciler:
    """FASE 9.5+ reconciler singleton wired to the live trader.

    Polls open orders on Binance Testnet every 5 min via
    ``reconcile_job`` and on operator demand via ``/reconcile``.
    """
    global _reconciler  # noqa: PLW0603
    if _reconciler is None:
        _reconciler = Reconciler(
            trader=get_ccxt_trader(),
            portfolio_state=get_portfolio_state(),
            order_repo=get_order_repository(),
            session_factory=async_session_factory,
        )
    return _reconciler


def get_portfolio_state() -> PortfolioState:
    """FASE 8.2+ portfolio cache, refreshed by `portfolio_sync_job`."""
    global _portfolio_state  # noqa: PLW0603
    if _portfolio_state is None:
        _portfolio_state = PortfolioState(trader=get_ccxt_trader())
    return _portfolio_state


def get_trading_state_service() -> TradingStateService:
    """FASE 8.3+ singleton ``trading_state`` row reader/updater."""
    global _trading_state_service  # noqa: PLW0603
    if _trading_state_service is None:
        _trading_state_service = TradingStateService(async_session_factory)
    return _trading_state_service


def get_risk_decision_repository() -> RiskDecisionRepository:
    """FASE 8.3+ append-only repository for ``risk_decisions``."""
    global _risk_decision_repo  # noqa: PLW0603
    if _risk_decision_repo is None:
        _risk_decision_repo = RiskDecisionRepository(async_session_factory)
    return _risk_decision_repo


def get_correlation_groups() -> CorrelationGroups:
    """Cached load of ``config/correlation_groups.yaml`` (FASE 8.4b)."""
    from pathlib import Path  # noqa: PLC0415

    return CorrelationGroups.from_yaml(Path("config/correlation_groups.yaml"))


def get_risk_manager() -> RiskManager:
    """FASE 8.3+ orchestrator. Gates registered in priority order
    (cheapest reject first). Each FASE 8.4 sub-commit appends the
    next gate behind the kill switch + DD pair.
    """
    global _risk_manager  # noqa: PLW0603
    if _risk_manager is None:
        state = get_trading_state_service()
        signals_repo = get_signal_repository()
        decisions_repo = get_risk_decision_repository()
        gates: list[Gate] = [
            KillSwitchGate(state),
            DailyDrawdownGate(state, async_session_factory),
            ExposurePerTickerGate(signals_repo, decisions_repo),
            CorrelationGroupGate(
                get_correlation_groups(), signals_repo, decisions_repo
            ),
            MaxConcurrentTradesGate(signals_repo),
            SignalsPerHourRateLimitGate(async_session_factory),
        ]
        _risk_manager = RiskManager(gates=gates, sizer=PositionSizer())
    return _risk_manager


async def shutdown_sources() -> None:
    """Close long-lived connections (called from FastAPI lifespan)."""
    if _ccxt is not None:
        await _ccxt.close()
    if _ccxt_trader is not None:
        await _ccxt_trader.close()
