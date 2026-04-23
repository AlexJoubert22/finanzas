"""Unit tests for CoinGeckoSource using respx to mock httpx."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_global(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.coingecko import CoinGeckoSource

    respx.get("https://api.coingecko.com/api/v3/global").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "total_market_cap": {"usd": 2.5e12},
                    "total_volume": {"usd": 1.2e11},
                    "market_cap_percentage": {"btc": 54.3, "eth": 18.9},
                    "active_cryptocurrencies": 12345,
                }
            },
        )
    )

    src = CoinGeckoSource()
    out = await src.fetch_global()
    assert out["btc_dominance_pct"] == pytest.approx(54.3)
    assert out["eth_dominance_pct"] == pytest.approx(18.9)
    assert out["total_market_cap_usd"] == pytest.approx(2.5e12)


@pytest.mark.asyncio
@respx.mock
async def test_coingecko_trending(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.coingecko import CoinGeckoSource

    respx.get("https://api.coingecko.com/api/v3/search/trending").mock(
        return_value=httpx.Response(
            200,
            json={
                "coins": [
                    {"item": {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin", "market_cap_rank": 1}},
                    {"item": {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "market_cap_rank": 2}},
                ]
            },
        )
    )

    src = CoinGeckoSource()
    out = await src.fetch_trending(limit=2)
    assert len(out) == 2
    assert out[0]["symbol"] == "BTC"
    assert out[1]["name"] == "Ethereum"
