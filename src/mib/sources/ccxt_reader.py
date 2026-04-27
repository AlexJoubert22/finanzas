"""CCXT-backed crypto data source — read-only side of the split.

Uses ``ccxt.async_support`` to query public endpoints of cryptocurrency
exchanges. Binance is the default venue because of generous public
rate limits and broad pair coverage.

This module is the read-only half of the trading split (FASE 7+
prep): it must NEVER carry exchange API keys with order permissions.
The write side lives in :mod:`mib.sources.ccxt_trader`.

**Lazy import** (spec FASE 5 pre-polish): ``ccxt.async_support`` eagerly
registers 100+ exchange classes at import time (~50 MiB RSS). We defer
the import to the first call so the uvicorn startup doesn't pay that
cost, and we import the single exchange submodule directly instead of
the whole aggregator.

All calls use ``asyncio.to_thread``-free paths because ccxt's async
client already uses aiohttp under the hood; the only reason we wrap
anything is the sync bootstrap in ``_get_exchange``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mib.logger import logger
from mib.models.market import Candle, Quote
from mib.sources.base import DataSource, RateLimiter, SourceError

# TYPE_CHECKING ensures this import runs only under mypy, not at runtime.
if TYPE_CHECKING:  # pragma: no cover
    import ccxt.async_support as ccxt_async  # noqa: F401

# TTLs match spec §4.
_TTL_TICKER_SEC = 30
_TTL_OHLCV_SEC = 30


class CCXTReader(DataSource):
    """Spot-market data from public CCXT endpoints.

    Attributes:
        exchange_id: CCXT exchange ID (``"binance"``, ``"kraken"`` …).
    """

    name: ClassVar[str] = "ccxt"

    def __init__(self, exchange_id: str = "binance") -> None:
        # Binance public endpoints allow ~1200 req/min; we stay well below.
        super().__init__(rate_limiter=RateLimiter(max_calls=20, period_seconds=60.0))
        self._exchange_id = exchange_id
        # Typed as Any to avoid triggering the ccxt import at class-load time.
        self._exchange: Any = None

    def _get_exchange(self) -> Any:
        """Lazy bootstrap of the exchange client on first call."""
        if self._exchange is None:
            # Importing the specific submodule avoids the top-level
            # `ccxt.async_support.__init__` which eagerly registers every
            # exchange class (Bybit, Kraken, Bitfinex, 100+ more).
            import importlib  # noqa: PLC0415

            try:
                mod = importlib.import_module(
                    f"ccxt.async_support.{self._exchange_id}"
                )
                cls = getattr(mod, self._exchange_id)
            except (ImportError, AttributeError) as exc:
                raise SourceError(f"Unknown CCXT exchange: {self._exchange_id}") from exc
            self._exchange = cls({"enableRateLimit": True})
        return self._exchange

    async def close(self) -> None:
        """Close the underlying aiohttp session. Call on app shutdown."""
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.warning("ccxt close failed: {}", exc)
            self._exchange = None

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_quote(self, symbol: str) -> Quote:
        """Return the latest ticker snapshot for ``symbol`` (``BTC/USDT``)."""
        key = f"ccxt:{self._exchange_id}:ticker:{symbol}"

        async def loader() -> dict[str, Any]:
            ex = self._get_exchange()
            ticker = await ex.fetch_ticker(symbol)
            return cast(dict[str, Any], ticker)

        raw = await self._cached_call(
            cache_key=key,
            ttl_seconds=_TTL_TICKER_SEC,
            endpoint=f"fetch_ticker:{symbol}",
            loader=loader,
        )
        # ccxt normalises to `{'last': ..., 'percentage': ..., 'quoteVolume': ...}`.
        last = _pick_number(raw, ("last", "close", "bid", "ask"))
        if last is None:
            raise SourceError(f"ccxt: no price found in ticker payload for {symbol}")
        ts_ms = raw.get("timestamp")
        ts = (
            datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            if isinstance(ts_ms, int | float)
            else datetime.now(UTC)
        )
        return Quote(
            ticker=symbol,
            kind="crypto",
            source=f"ccxt:{self._exchange_id}",
            price=float(last),
            change_24h_pct=(
                float(raw["percentage"])
                if raw.get("percentage") is not None
                else None
            ),
            currency=symbol.split("/")[-1] if "/" in symbol else None,
            venue=self._exchange_id,
            timestamp=ts,
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100,
    ) -> list[Candle]:
        """Return the last ``limit`` OHLCV bars for ``symbol``."""
        key = f"ccxt:{self._exchange_id}:ohlcv:{symbol}:{timeframe}:{limit}"

        async def loader() -> list[list[float]]:
            ex = self._get_exchange()
            bars = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            # ccxt returns list-of-lists: [ts_ms, open, high, low, close, volume]
            return cast(list[list[float]], bars)

        raw = await self._cached_call(
            cache_key=key,
            ttl_seconds=_TTL_OHLCV_SEC,
            endpoint=f"fetch_ohlcv:{symbol}:{timeframe}:{limit}",
            loader=loader,
        )
        return [
            Candle(
                timestamp=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in raw
        ]

    async def health(self) -> bool:
        """Lightweight liveness probe: fetch a well-known ticker."""
        try:
            await self.fetch_quote("BTC/USDT")
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means degraded
            logger.warning("ccxt health probe failed: {}", exc)
            return False


def _pick_number(d: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first non-null numeric value from ``d`` among ``keys``."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, int | float):
            return float(v)
    return None
