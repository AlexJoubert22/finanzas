"""Alpha Vantage — fundamentals and overview data for US-listed companies.

Free tier: 25 req/DAY, 5/min. We rate-limit at 5/min to honour the
minute cap, and the service layer is responsible for bounding daily
usage by caching (TTL 24 h per spec §4) and by only hitting AV on
explicit ``/fundamentals`` requests in phase 4+, never on the critical
path of ``/symbol``.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

import httpx

from mib.config import get_settings
from mib.logger import logger
from mib.sources.base import DataSource, RateLimiter, SourceError

_BASE_URL = "https://www.alphavantage.co/query"
_TTL_SEC = 24 * 3600  # spec §4 — fundamentals barely change day-to-day
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


class AlphaVantageSource(DataSource):
    name: ClassVar[str] = "alphavantage"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=5, period_seconds=60.0))
        self._api_key = get_settings().alpha_vantage_api_key

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        if not self._api_key:
            raise SourceError("alphavantage: ALPHA_VANTAGE_API_KEY not set")
        params = {**params, "apikey": self._api_key}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(_BASE_URL, params=params)
            r.raise_for_status()
            data = cast(dict[str, Any], r.json())
        # AV returns {"Note": "..."} when quota is hit and {"Information": "..."}
        # on plan upgrade prompts. Treat both as soft-fail signals.
        if "Note" in data:
            raise SourceError(f"alphavantage: quota — {data['Note']}")
        if "Information" in data:
            raise SourceError(f"alphavantage: info — {data['Information']}")
        return data

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_overview(self, ticker: str) -> dict[str, Any]:
        """Company overview — market cap, P/E, dividend yield, sector, etc.

        Returns the subset of fields most commonly used downstream:
        - ``Symbol``, ``Name``, ``Sector``, ``Industry``
        - ``MarketCapitalization`` (int), ``PERatio`` (float), ``DividendYield`` (float)
        - ``EPS`` (float), ``52WeekHigh`` / ``52WeekLow`` (float)
        """
        async def loader() -> dict[str, Any]:
            return await self._get({"function": "OVERVIEW", "symbol": ticker})

        raw = await self._cached_call(
            cache_key=f"alphavantage:overview:{ticker}",
            ttl_seconds=_TTL_SEC,
            endpoint=f"OVERVIEW:{ticker}",
            loader=loader,
        )
        if not raw or raw.get("Symbol", "").upper() != ticker.upper():
            raise SourceError(f"alphavantage: no overview data for {ticker}")

        def _f(key: str) -> float | None:
            v = raw.get(key)
            if v in (None, "", "None", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _i(key: str) -> int | None:
            f = _f(key)
            return int(f) if f is not None else None

        return {
            "symbol": raw.get("Symbol"),
            "name": raw.get("Name"),
            "sector": raw.get("Sector"),
            "industry": raw.get("Industry"),
            "exchange": raw.get("Exchange"),
            "currency": raw.get("Currency"),
            "market_cap": _i("MarketCapitalization"),
            "pe_ratio": _f("PERatio"),
            "eps": _f("EPS"),
            "dividend_yield": _f("DividendYield"),
            "high_52w": _f("52WeekHigh"),
            "low_52w": _f("52WeekLow"),
            "description": raw.get("Description"),
        }

    async def health(self) -> bool:
        """Probe with an OVERVIEW for AAPL — costs 1 of our 25/day."""
        if not self._api_key:
            return False
        try:
            await self.fetch_overview("AAPL")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("alphavantage health probe failed: {}", exc)
            return False
