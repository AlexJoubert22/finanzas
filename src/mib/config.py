"""Application configuration via pydantic-settings.

All settings are loaded from environment variables. The `.env` file
in the project root is read automatically by pydantic-settings.

Usage:
    from mib.config import get_settings
    settings = get_settings()
"""

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
