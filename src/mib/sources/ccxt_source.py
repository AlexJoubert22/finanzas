"""CCXT-backed crypto data source.

Uses ``ccxt.async_support`` to query public endpoints of cryptocurrency
exchanges. Binance is the default venue because of generous public
rate limits and broad pair coverage.

All calls use ``to_thread`` under the hood because ccxt's async API is
somewhat leaky with sync side-effects. The call site of ``fetch_ticker``
etc. is the public coroutine (no sync blocking in handlers).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import ccxt.async_support as ccxt_async

from mib.logger import logger
from mib.models.market import Candle, Quote
from mib.sources.base import DataSource, RateLimiter, SourceError

# TTLs match spec §4.
_TTL_TICKER_SEC = 30
_TTL_OHLCV_SEC = 30


class CCXTSource(DataSource):
    """Spot-market data from public CCXT endpoints.

    Attributes:
        exchange_id: CCXT exchange ID (``"binance"``, ``"kraken"`` …).
    """

    name: ClassVar[str] = "ccxt"

    def __init__(self, exchange_id: str = "binance") -> None:
        # Binance public endpoints allow ~1200 req/min; we stay well below.
        super().__init__(rate_limiter=RateLimiter(max_calls=20, period_seconds=60.0))
        self._exchange_id = exchange_id
        self._exchange: ccxt_async.Exchange | None = None

    def _get_exchange(self) -> ccxt_async.Exchange:
        if self._exchange is None:
            cls = getattr(ccxt_async, self._exchange_id, None)
            if cls is None:
                raise SourceError(f"Unknown CCXT exchange: {self._exchange_id}")
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
