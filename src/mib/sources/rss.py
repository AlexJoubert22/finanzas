"""RSS aggregator — Reuters, CoinDesk, MarketWatch, SEC EDGAR…

Feeds are configured in ``config/rss_feeds.yaml`` (read lazily per call
so hot-reloading the file is enough — no restart needed per spec §1.10).

No API keys. Only rate-limited client-side: 30 calls/min.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import feedparser  # type: ignore[import-untyped]
import httpx
import yaml

from mib.logger import logger
from mib.sources.base import DataSource, RateLimiter, SourceError

_TTL_SEC = 600  # spec §4 — 10 min
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# Bundled default so the file is optional at first.
_DEFAULT_FEEDS = {
    "feeds": [
        {"name": "Reuters Business", "url": "https://www.reuters.com/markets/feed/"},
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss"},
        {"name": "MarketWatch Top", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    ]
}


class RSSSource(DataSource):
    name: ClassVar[str] = "rss"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=30, period_seconds=60.0))
        self._config_path = Path(config_path) if config_path else Path("config/rss_feeds.yaml")

    def _load_feeds(self) -> list[dict[str, str]]:
        """Hot-reload feed list from YAML each call; fall back to bundled defaults."""
        try:
            if self._config_path.is_file():
                with self._config_path.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                return cast(list[dict[str, str]], data.get("feeds", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("rss: failed to read {}, using defaults: {}", self._config_path, exc)
        return cast(list[dict[str, str]], _DEFAULT_FEEDS["feeds"])

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_feed(self, url: str, *, limit: int = 15) -> list[dict[str, Any]]:
        """Download one RSS feed and return parsed entries."""
        cache_key = f"rss:{hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:12]}"

        async def loader() -> list[dict[str, Any]]:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                text = r.text
            parsed = await asyncio.to_thread(feedparser.parse, text)
            out: list[dict[str, Any]] = []
            for entry in parsed.entries[:limit]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                ts = (
                    datetime(*pub[:6], tzinfo=UTC).isoformat()
                    if pub
                    else datetime.now(UTC).isoformat()
                )
                out.append(
                    {
                        "title": entry.get("title", "").strip(),
                        "link": entry.get("link"),
                        "summary": entry.get("summary", "").strip(),
                        "author": entry.get("author"),
                        "published_at": ts,
                    }
                )
            return out

        try:
            return await self._cached_call(
                cache_key=cache_key, ttl_seconds=_TTL_SEC, endpoint=f"feed:{url}", loader=loader
            )
        except SourceError:
            logger.info("rss: feed unreachable, skipping — {}", url)
            return []

    async def fetch_all(self, limit_per_feed: int = 10) -> list[dict[str, Any]]:
        """Aggregate all configured feeds; each feed failure is isolated."""
        feeds = self._load_feeds()
        tasks = [
            self.fetch_feed(f["url"], limit=limit_per_feed)
            for f in feeds
            if f.get("url")
        ]
        results: list[list[dict[str, Any]]] = await asyncio.gather(*tasks, return_exceptions=False)
        out: list[dict[str, Any]] = []
        for feed_cfg, entries in zip(feeds, results, strict=True):
            for e in entries:
                e["feed"] = feed_cfg.get("name", "rss")
                out.append(e)
        # Latest first.
        out.sort(key=lambda e: e.get("published_at", ""), reverse=True)
        return out

    async def health(self) -> bool:
        # We consider RSS 'ok' if at least one feed returns anything.
        try:
            items = await self.fetch_all(limit_per_feed=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rss health probe failed: {}", exc)
            return False
        return len(items) > 0
