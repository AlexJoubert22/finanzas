"""Unit tests for YFinanceSource using monkeypatched sync helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_yf_fetch_quote_computes_change_pct(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources.yfinance_source import YFinanceSource

    def _sync(ticker: str) -> dict[str, Any]:  # noqa: ARG001
        return {
            "last_price": 200.0,
            "previous_close": 160.0,
            "currency": "USD",
            "exchange": "NASDAQ",
        }

    monkeypatch.setattr(YFinanceSource, "_sync_fetch_quote", staticmethod(_sync))

    src = YFinanceSource()
    quote = await src.fetch_quote("AAPL")

    assert quote.ticker == "AAPL"
    assert quote.kind == "stock"
    assert quote.source == "yfinance"
    assert quote.price == pytest.approx(200.0)
    # (200-160)/160 * 100 = 25 %
    assert quote.change_24h_pct == pytest.approx(25.0)
    assert quote.currency == "USD"
    assert quote.venue == "NASDAQ"


@pytest.mark.asyncio
async def test_yf_fetch_ohlcv_parses_rows(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources.yfinance_source import YFinanceSource

    def _sync(
        ticker: str, interval: str, period: str, limit: int  # noqa: ARG001
    ) -> list[dict[str, Any]]:
        ts1 = datetime(2026, 4, 22, 12, tzinfo=UTC).isoformat()
        ts2 = datetime(2026, 4, 22, 13, tzinfo=UTC).isoformat()
        return [
            {
                "timestamp": ts1,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 103.0,
                "volume": 10000.0,
            },
            {
                "timestamp": ts2,
                "open": 103.0,
                "high": 110.0,
                "low": 101.0,
                "close": 108.0,
                "volume": 15000.0,
            },
        ]

    monkeypatch.setattr(YFinanceSource, "_sync_fetch_ohlcv", staticmethod(_sync))

    src = YFinanceSource()
    candles = await src.fetch_ohlcv("AAPL", timeframe="1h", limit=2)

    assert len(candles) == 2
    assert candles[0].open == 100.0
    assert candles[1].high == 110.0
