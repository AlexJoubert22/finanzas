"""News aggregation service.

For a specific ticker: ``Finnhub /company-news`` (authoritative source).
Fallback: RSS feeds filtered by a case-insensitive search on the
ticker appearing in the headline (best-effort when Finnhub quota is
exhausted).

Sentiment will be attached in phase 4 by the AI router; this module
stays free of AI concerns and just returns the raw headlines.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mib.logger import logger
from mib.models.news import NewsItem, NewsResponse
from mib.sources.finnhub import FinnhubSource
from mib.sources.rss import RSSSource


class NewsService:
    def __init__(self, finnhub: FinnhubSource, rss: RSSSource) -> None:
        self._finnhub = finnhub
        self._rss = rss

    async def for_ticker(self, ticker: str, *, limit: int = 10) -> NewsResponse:
        items: list[NewsItem] = []
        try:
            raw = await self._finnhub.fetch_company_news(ticker, days_back=3, limit=limit)
            for r in raw:
                items.append(
                    NewsItem(
                        headline=r["headline"],
                        url=r.get("url"),
                        source=r.get("source") or "finnhub",
                        summary=r.get("summary") or "",
                        published_at=datetime.fromisoformat(r["published_at"]),
                        ticker=ticker.upper(),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("news: finnhub failed for {}: {}", ticker, exc)

        if not items:
            # Fall back to RSS and filter client-side.
            logger.info("news: falling back to RSS for {}", ticker)
            items = await self._rss_fallback(ticker, limit)

        return NewsResponse(
            ticker=ticker.upper(),
            items=items[:limit],
            generated_at=datetime.now(UTC),
        )

    async def market_stream(self, limit: int = 15) -> NewsResponse:
        """General market news — Finnhub ``/news?category=general``."""
        items: list[NewsItem] = []
        try:
            raw = await self._finnhub.fetch_market_news(limit=limit)
            items = [
                NewsItem(
                    headline=r["headline"],
                    url=r.get("url"),
                    source=r.get("source") or "finnhub",
                    summary=r.get("summary") or "",
                    published_at=datetime.fromisoformat(r["published_at"]),
                )
                for r in raw
            ]
        except Exception as exc:  # noqa: BLE001
            logger.info("news: market stream fallback to RSS: {}", exc)
            items = await self._rss_fallback(None, limit)
        return NewsResponse(
            ticker=None,
            items=items[:limit],
            generated_at=datetime.now(UTC),
        )

    async def _rss_fallback(self, ticker: str | None, limit: int) -> list[NewsItem]:
        try:
            rss_items = await self._rss.fetch_all(limit_per_feed=8)
        except Exception as exc:  # noqa: BLE001
            logger.warning("news: RSS aggregation failed: {}", exc)
            return []
        needle = ticker.upper() if ticker else None
        out: list[NewsItem] = []
        for it in rss_items:
            title = it.get("title", "")
            if needle and needle not in title.upper():
                continue
            out.append(
                NewsItem(
                    headline=title,
                    url=it.get("link"),
                    source=it.get("feed", "rss"),
                    summary=it.get("summary") or "",
                    published_at=datetime.fromisoformat(it["published_at"]),
                    ticker=needle,
                )
            )
            if len(out) >= limit:
                break
        return out
