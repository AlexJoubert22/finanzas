"""Finnhub — company news with sentiment signals.

Free tier: 60 calls/min. We cap at 50/min to leave headroom for the
other sources sharing our outbound bandwidth.

Endpoints we use:
- ``/news?category=general`` — breaking financial news feed.
- ``/company-news?symbol=X&from=&to=`` — ticker-specific news.

Sentiment is attached later by the AI router in Fase 4; this source
just brings the raw items.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

import httpx

from mib.config import get_settings
from mib.logger import logger
from mib.sources.base import DataSource, RateLimiter, SourceError

_BASE_URL = "https://finnhub.io/api/v1"
_TTL_SEC = 300  # spec §4 — 5 min
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


class FinnhubSource(DataSource):
    name: ClassVar[str] = "finnhub"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=50, period_seconds=60.0))
        self._api_key = get_settings().finnhub_api_key

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._api_key:
            raise SourceError("finnhub: FINNHUB_API_KEY not set")
        params = {**(params or {}), "token": self._api_key}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(f"{_BASE_URL}{path}", params=params)
            r.raise_for_status()
            return r.json()

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_company_news(
        self, ticker: str, *, days_back: int = 3, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Latest headlines for ``ticker`` over the past ``days_back`` days."""
        now = datetime.now(UTC)
        since = (now - timedelta(days=days_back)).date().isoformat()
        until = now.date().isoformat()

        async def loader() -> list[dict[str, Any]]:
            return cast(
                list[dict[str, Any]],
                await self._get(
                    "/company-news",
                    params={"symbol": ticker.upper(), "from": since, "to": until},
                ),
            )

        raw = await self._cached_call(
            cache_key=f"finnhub:company-news:{ticker}:{since}",
            ttl_seconds=_TTL_SEC,
            endpoint=f"company-news:{ticker}",
            loader=loader,
        )
        items = raw[:limit]
        return [
            {
                "id": str(it.get("id", "")),
                "headline": it.get("headline", "").strip(),
                "source": it.get("source"),
                "url": it.get("url"),
                "summary": (it.get("summary") or "").strip(),
                "image": it.get("image"),
                "ticker": ticker.upper(),
                "published_at": datetime.fromtimestamp(
                    int(it["datetime"]), tz=UTC
                ).isoformat(),
            }
            for it in items
            if it.get("datetime") and it.get("headline")
        ]

    async def fetch_market_news(self, category: str = "general", limit: int = 10) -> list[dict[str, Any]]:
        """Broad market news stream (``/news?category=general``)."""
        async def loader() -> list[dict[str, Any]]:
            return cast(
                list[dict[str, Any]],
                await self._get("/news", params={"category": category}),
            )

        raw = await self._cached_call(
            cache_key=f"finnhub:market-news:{category}",
            ttl_seconds=_TTL_SEC,
            endpoint=f"news:{category}",
            loader=loader,
        )
        return [
            {
                "id": str(it.get("id", "")),
                "headline": it.get("headline", "").strip(),
                "source": it.get("source"),
                "url": it.get("url"),
                "summary": (it.get("summary") or "").strip(),
                "image": it.get("image"),
                "category": it.get("category", category),
                "published_at": datetime.fromtimestamp(
                    int(it["datetime"]), tz=UTC
                ).isoformat(),
            }
            for it in raw[:limit]
            if it.get("datetime") and it.get("headline")
        ]

    async def health(self) -> bool:
        if not self._api_key:
            return False
        try:
            # Cheap probe: fetch the general market news (1 call).
            await self.fetch_market_news(limit=1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("finnhub health probe failed: {}", exc)
            return False
