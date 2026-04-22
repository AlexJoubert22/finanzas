"""Yahoo Finance (yfinance) data source for stocks, ETFs, forex, indices.

``yfinance`` is synchronous; we wrap every call with ``asyncio.to_thread``
to keep handlers non-blocking. Cache TTL for quotes is 60 s (spec §4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import yfinance as yf  # type: ignore[import-untyped]

from mib.logger import logger
from mib.models.market import Candle, Quote
from mib.sources.base import DataSource, RateLimiter, SourceError

_TTL_QUOTE_SEC = 60
_TTL_OHLCV_SEC = 60


# Map our "1h"/"4h"/"1d"/… nomenclature to yfinance's interval strings.
_TF_TO_YF = {
    "1m": ("1m", "1d"),
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "30m": ("30m", "1mo"),
    "1h": ("60m", "1mo"),
    "4h": ("60m", "6mo"),  # yfinance doesn't natively expose 4h — we resample in FASE 3
    "1d": ("1d", "6mo"),
    "1wk": ("1wk", "5y"),
}


class YFinanceSource(DataSource):
    """Stocks / ETF / forex / index quotes and bars from Yahoo Finance."""

    name: ClassVar[str] = "yfinance"

    def __init__(self) -> None:
        # yfinance has no documented free-tier limit; we still cap at 30/min
        # to be polite and avoid ad-hoc 429s from Yahoo.
        super().__init__(rate_limiter=RateLimiter(max_calls=30, period_seconds=60.0))

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_quote(self, ticker: str) -> Quote:
        """Fetch the latest quote for ``ticker`` (``AAPL``, ``^GSPC`` …)."""
        key = f"yfinance:quote:{ticker}"

        async def loader() -> dict[str, Any]:
            return await asyncio.to_thread(self._sync_fetch_quote, ticker)

        raw = await self._cached_call(
            cache_key=key,
            ttl_seconds=_TTL_QUOTE_SEC,
            endpoint=f"fast_info:{ticker}",
            loader=loader,
        )
        price = raw.get("last_price")
        if price is None:
            raise SourceError(f"yfinance: no last_price for {ticker}")
        previous_close = raw.get("previous_close")
        change_pct: float | None = None
        if (
            isinstance(previous_close, int | float)
            and isinstance(price, int | float)
            and previous_close
        ):
            change_pct = (float(price) - float(previous_close)) / float(previous_close) * 100.0

        return Quote(
            ticker=ticker,
            kind="stock",
            source="yfinance",
            price=float(price),
            change_24h_pct=change_pct,
            currency=raw.get("currency"),
            venue=raw.get("exchange"),
            timestamp=datetime.now(UTC),
        )

    async def fetch_ohlcv(
        self,
        ticker: str,
        timeframe: str = "1h",
        limit: int = 100,
    ) -> list[Candle]:
        """Return up to ``limit`` most-recent bars for ``ticker``."""
        yf_interval, yf_period = _TF_TO_YF.get(timeframe, _TF_TO_YF["1h"])
        key = f"yfinance:ohlcv:{ticker}:{yf_interval}:{yf_period}:{limit}"

        async def loader() -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                self._sync_fetch_ohlcv, ticker, yf_interval, yf_period, limit
            )

        raw = await self._cached_call(
            cache_key=key,
            ttl_seconds=_TTL_OHLCV_SEC,
            endpoint=f"history:{ticker}:{yf_interval}:{yf_period}",
            loader=loader,
        )
        return [
            Candle(
                timestamp=datetime.fromisoformat(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            for row in raw
        ]

    async def health(self) -> bool:
        """Lightweight liveness probe: fetch SPY last price."""
        try:
            await self.fetch_quote("SPY")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance health probe failed: {}", exc)
            return False

    # ─── Sync helpers (run inside ``asyncio.to_thread``) ───────────────

    @staticmethod
    def _sync_fetch_quote(ticker: str) -> dict[str, Any]:
        t = yf.Ticker(ticker)
        # fast_info is ~100× faster than .info (no full fundamentals pull).
        fi = t.fast_info
        # fast_info exposes dict-like read; copy the fields we care about to
        # avoid serialising yfinance internals to the cache.
        out: dict[str, Any] = {}
        for attr in ("last_price", "previous_close", "currency", "exchange"):
            try:
                out[attr] = getattr(fi, attr)
            except (AttributeError, KeyError):
                out[attr] = None
        return out

    @staticmethod
    def _sync_fetch_ohlcv(
        ticker: str, interval: str, period: str, limit: int
    ) -> list[dict[str, Any]]:
        t = yf.Ticker(ticker)
        df = t.history(interval=interval, period=period, auto_adjust=False)
        if df is None or df.empty:
            return []
        # tail(limit) keeps memory low and enforces the cap.
        df = df.tail(limit)
        out: list[dict[str, Any]] = []
        for ts, row in df.iterrows():
            ts_iso = ts.to_pydatetime().astimezone(UTC).isoformat()
            out.append(
                {
                    "timestamp": ts_iso,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]) if row.get("Volume") is not None else 0.0,
                }
            )
        return cast(list[dict[str, Any]], out)
