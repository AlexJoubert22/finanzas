"""Unit tests for AlphaVantageSource."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.mark.asyncio
@respx.mock
async def test_alphavantage_overview_parses_fields(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.alphavantage import AlphaVantageSource

    src = AlphaVantageSource()
    src._api_key = "fake"  # noqa: SLF001

    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "Symbol": "AAPL",
                "Name": "Apple Inc.",
                "Sector": "Technology",
                "Industry": "Consumer Electronics",
                "Exchange": "NASDAQ",
                "Currency": "USD",
                "MarketCapitalization": "3000000000000",
                "PERatio": "28.5",
                "EPS": "6.20",
                "DividendYield": "0.0045",
                "52WeekHigh": "300",
                "52WeekLow": "180",
                "Description": "Apple designs and manufactures consumer electronics.",
            },
        )
    )

    out = await src.fetch_overview("AAPL")
    assert out["symbol"] == "AAPL"
    assert out["market_cap"] == 3_000_000_000_000
    assert out["pe_ratio"] == pytest.approx(28.5)
    assert out["eps"] == pytest.approx(6.20)
    assert out["high_52w"] == pytest.approx(300.0)


@pytest.mark.asyncio
@respx.mock
async def test_alphavantage_overview_quota_warning_raises(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.alphavantage import AlphaVantageSource
    from mib.sources.base import SourceError

    src = AlphaVantageSource()
    src._api_key = "fake"  # noqa: SLF001

    respx.get("https://www.alphavantage.co/query").mock(
        return_value=httpx.Response(
            200,
            json={"Note": "Thank you for using Alpha Vantage! Daily rate limit reached."},
        )
    )
    with pytest.raises(SourceError, match="quota"):
        await src.fetch_overview("AAPL")
