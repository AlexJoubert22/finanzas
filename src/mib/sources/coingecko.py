"""CoinGecko — crypto market cap, trending, dominance.

Free API tier: ~10-30 req/min (undocumented exactly, varies). We cap at
10/min to stay safely under. API key header is optional but doubles the
quota — we send it when present in the env.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

import httpx

from mib.config import get_settings
from mib.logger import logger
from mib.sources.base import DataSource, RateLimiter

_BASE_URL = "https://api.coingecko.com/api/v3"
_TTL_SEC = 120  # spec §4

# httpx timeout profile — CG sometimes stalls briefly.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


class CoinGeckoSource(DataSource):
    name: ClassVar[str] = "coingecko"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=10, period_seconds=60.0))
        self._api_key = get_settings().coingecko_api_key or None

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json", "user-agent": "mib/0.1 (+https://github.com/)"}
        if self._api_key:
            # CoinGecko public free tier uses `x-cg-demo-api-key`.
            h["x-cg-demo-api-key"] = self._api_key
        return h

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(
            base_url=_BASE_URL, timeout=_HTTP_TIMEOUT, headers=self._headers()
        ) as client:
            r = await client.get(path, params=params)
            r.raise_for_status()
            return r.json()

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_global(self) -> dict[str, Any]:
        """`/global` — total market cap, BTC dominance, volume 24h…"""
        async def loader() -> dict[str, Any]:
            return cast(dict[str, Any], await self._get("/global"))

        raw = await self._cached_call(
            cache_key="coingecko:global",
            ttl_seconds=_TTL_SEC,
            endpoint="global",
            loader=loader,
        )
        data = raw.get("data", raw)
        return {
            "total_market_cap_usd": float(data["total_market_cap"]["usd"]),
            "total_volume_24h_usd": float(data["total_volume"]["usd"]),
            "btc_dominance_pct": float(data["market_cap_percentage"]["btc"]),
            "eth_dominance_pct": float(data["market_cap_percentage"].get("eth", 0.0)),
            "active_cryptocurrencies": int(data.get("active_cryptocurrencies", 0)),
        }

    async def fetch_trending(self, limit: int = 7) -> list[dict[str, Any]]:
        """`/search/trending` — top N tickers ranked by user interest."""
        async def loader() -> dict[str, Any]:
            return cast(dict[str, Any], await self._get("/search/trending"))

        raw = await self._cached_call(
            cache_key="coingecko:trending",
            ttl_seconds=_TTL_SEC,
            endpoint="search/trending",
            loader=loader,
        )
        items = raw.get("coins", [])[:limit]
        out: list[dict[str, Any]] = []
        for it in items:
            item = it.get("item", it)
            out.append(
                {
                    "id": item.get("id"),
                    "symbol": (item.get("symbol") or "").upper(),
                    "name": item.get("name"),
                    "market_cap_rank": item.get("market_cap_rank"),
                }
            )
        return out

    async def health(self) -> bool:
        try:
            await self._get("/ping")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("coingecko health probe failed: {}", exc)
            return False
