"""Unit tests for FinnhubSource."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.mark.asyncio
@respx.mock
async def test_finnhub_company_news(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    # Override env so the source thinks it has a key.
    from mib.config import get_settings

    monkeypatch.setattr(
        get_settings(), "finnhub_api_key", "fake-key", raising=False
    )
    # Freeze the Source to use our fake key.
    from mib.sources.finnhub import FinnhubSource

    src = FinnhubSource()
    src._api_key = "fake-key"  # noqa: SLF001 - controlled test override

    respx.get("https://finnhub.io/api/v1/company-news").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "datetime": 1_745_000_000,
                    "headline": "Apple beats expectations",
                    "source": "Reuters",
                    "url": "https://example.com/a",
                    "summary": "…",
                },
                {
                    "id": 2,
                    "datetime": 1_745_100_000,
                    "headline": "Apple announces new iPhone",
                    "source": "Bloomberg",
                    "url": "https://example.com/b",
                    "summary": "…",
                },
            ],
        )
    )

    items = await src.fetch_company_news("AAPL", days_back=3, limit=5)
    assert len(items) == 2
    assert items[0]["ticker"] == "AAPL"
    assert "Apple" in items[0]["headline"]
