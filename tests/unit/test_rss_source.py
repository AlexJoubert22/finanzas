"""Unit tests for RSSSource."""

from __future__ import annotations

import httpx
import pytest
import respx

_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample Feed</title>
    <item>
      <title>Stocks rally on earnings</title>
      <link>https://example.com/1</link>
      <description>Summary here.</description>
      <pubDate>Wed, 23 Apr 2026 12:00:00 GMT</pubDate>
      <author>jane@example.com</author>
    </item>
    <item>
      <title>Crypto markets stable</title>
      <link>https://example.com/2</link>
      <description>Another summary.</description>
      <pubDate>Wed, 23 Apr 2026 11:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


@pytest.mark.asyncio
@respx.mock
async def test_rss_fetch_feed_parses_entries(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.rss import RSSSource

    respx.get("https://fake.example.com/feed").mock(
        return_value=httpx.Response(
            200,
            text=_SAMPLE_FEED,
            headers={"content-type": "application/rss+xml"},
        )
    )

    src = RSSSource()
    items = await src.fetch_feed("https://fake.example.com/feed", limit=5)
    assert len(items) == 2
    assert items[0]["title"] == "Stocks rally on earnings"
    assert items[0]["link"] == "https://example.com/1"
    assert items[1]["title"] == "Crypto markets stable"
