"""Application configuration via pydantic-settings.

All settings are loaded from environment variables. The `.env` file
in the project root is read automatically by pydantic-settings.

Usage:
    from mib.config import get_settings
    settings = get_settings()
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mib.trading.mode import TradingMode


class Settings(BaseSettings):
    """Runtime configuration loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── App ────────────────────────────────────────────────────────
    app_env: Literal["production", "development", "test"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    timezone: str = "Europe/Madrid"

    # ─── Database ───────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/mib.db"

    # ─── Telegram ───────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""
    # Chat ID where scheduled scanner jobs and the daily report send
    # their cards. ``0`` disables the scheduler-side delivery (the
    # /scan ad-hoc handler and /paper_status still work).
    operator_telegram_id: int = 0

    # ─── API server ─────────────────────────────────────────────────
    # Default: loopback only (spec §13). Inside Docker containers this
    # is overridden to 0.0.0.0 via compose environment so docker-proxy
    # can reach the app; LAN exposure is prevented at the host side by
    # the port mapping `127.0.0.1:8000:8000` in docker-compose.yml.
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ─── AI providers ───────────────────────────────────────────────
    groq_api_key: str = ""
    groq_daily_limit: int = 14000

    openrouter_api_key: str = ""
    openrouter_daily_limit: int = 200

    gemini_api_key: str = ""
    gemini_daily_limit: int = 1500

    # NVIDIA Build (NIM API, OpenAI-compatible). Operator has 1-year
    # subscription. Default daily limit is conservative; raise once
    # actual usage shape is known.
    nvidia_api_key: str = ""
    nvidia_daily_limit: int = 10000
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # ─── Binance Testnet (sandbox, FASE 9.1) ────────────────────────
    # Separate from any real-account credentials. Withdrawals MUST
    # stay disabled on this key (sandbox doesn't have real funds, but
    # the same hygiene applies on day one). The base_url MUST contain
    # "testnet" or "sandbox" — the third seatbelt in CCXTTrader
    # rejects any writes whose target URL doesn't match this pattern.
    binance_sandbox_api_key: str = ""
    binance_sandbox_secret: str = ""
    binance_sandbox_base_url: str = "https://testnet.binance.vision"

    # ─── Data source keys ───────────────────────────────────────────
    alpha_vantage_api_key: str = ""
    finnhub_api_key: str = ""
    fred_api_key: str = ""
    coingecko_api_key: str = ""

    # ─── Scheduler intervals (seconds) ──────────────────────────────
    price_alerts_interval_sec: int = Field(default=60, ge=10)
    watchlist_interval_sec: int = Field(default=300, ge=60)
    news_monitor_interval_sec: int = Field(default=900, ge=60)

    # ─── Trading (FASE 7+) ──────────────────────────────────────────
    # Master kill switch: when False, no order leaves the process even
    # if the executor is invoked. The CCXTTrader also short-circuits on
    # this flag as a second seatbelt. Default False — flip to True only
    # after PAPER validation.
    trading_enabled: bool = False
    # Operational mode ladder; see ``mib.trading.mode.TradingMode``.
    trading_mode: TradingMode = TradingMode.OFF

    # ─── Risk gates (FASE 8.4+) ─────────────────────────────────────
    # Cap exposure to a single ticker: sum of realized notional + sized
    # pending signals must stay below this fraction of equity.
    max_exposure_per_ticker_pct: float = Field(default=0.15, gt=0.0, le=1.0)
    # Hard cap on simultaneously open positions (8.4c).
    max_concurrent_trades: int = Field(default=5, ge=1, le=50)
    # Rolling 1h cap on approved signals (8.4d). Defensive against
    # runaway signal generation from bugs or extreme regimes.
    max_signals_per_hour: int = Field(default=2, ge=1, le=100)
    # Risk-based sizing parameters (8.5). Defaults locked in
    # strategic session 2026-04-28; overrides require operator
    # confirmation in a future session, never tune at runtime.
    risk_per_trade_pct: float = Field(default=0.005, gt=0.0, le=0.05)
    # FASE 11.6 — opt-in MinAIConfidenceGate. False by default so the
    # FASE 8 chain is unchanged until the operator explicitly trusts
    # the AI Validator's confidence as a hard gate. When True, a gate
    # rejecting signals with confidence_ai < min_ai_confidence_threshold
    # is appended to the chain.
    risk_use_ai_confidence: bool = False
    min_ai_confidence_threshold: float = Field(
        default=0.55, ge=0.0, le=1.0
    )
    max_position_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    min_notional_quote: float = Field(default=10.0, ge=0.0)

    # ─── PAPER mode baseline (pre-PAPER tweak) ──────────────────────
    # Virtual capital baseline used in PAPER. When the testnet balance
    # falls below this (testnet resets, partial fills consumed cash),
    # PortfolioState pads equity_quote up to this value so sizing math
    # and PnL/% computations stay anchored to a stable reference.
    paper_initial_capital_quote: Decimal = Field(default=Decimal("6000.0"))

    # ─── Dead-man heartbeat (FASE 13.7) ─────────────────────────────
    # Token authenticating the public /heartbeat endpoint. Set to a
    # long random string in production; empty disables the endpoint
    # (returns 503 to avoid leaking that it exists without auth).
    heartbeat_token: str = ""
    heartbeat_scheduler_max_age_sec: int = Field(default=60, ge=10, le=600)
    heartbeat_reconcile_max_age_sec: int = Field(
        default=600, ge=60, le=3600
    )

    # ─── /go_live 2FA (FASE 14.2) ────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    operator_email: str = ""
    go_live_code_ttl_seconds: int = Field(default=300, ge=60, le=900)
    go_live_min_confirm_delay_seconds: int = Field(
        default=30, ge=10, le=120
    )
    go_live_max_attempts: int = Field(default=5, ge=1, le=20)

    # ─── First-30-days sizing (FASE 14.3) ────────────────────────────
    live_first_30_days_sizing_modifier: float = Field(
        default=0.5, gt=0.0, le=1.0
    )

    # ─── Runtime tuning ─────────────────────────────────────────────
    malloc_arena_max: int = 2

    # ─── Derived helpers ────────────────────────────────────────────
    @property
    def telegram_allowed_user_ids(self) -> set[int]:
        """Parse CSV string into a set of Telegram user IDs."""
        if not self.telegram_allowed_users:
            return set()
        return {
            int(uid.strip())
            for uid in self.telegram_allowed_users.split(",")
            if uid.strip().isdigit()
        }

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @field_validator("api_host")
    @classmethod
    def _api_host_allowed(cls, v: str) -> str:
        """Allow loopback OR 0.0.0.0 (Docker-only; LAN blocked by port map)."""
        allowed = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}  # noqa: S104
        if v not in allowed:
            raise ValueError(
                f"api_host must be one of {allowed} (got {v!r}). "
                "LAN exposure is prevented at the Docker port mapping level, "
                "not here — see docker-compose.yml (spec §11bis)."
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached factory — Settings are immutable after the first call."""
    return Settings()
