"""Unit tests for YFinanceSource using monkeypatched sync helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd
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
async def test_yf_fetch_quote_falls_back_to_history_when_prev_close_none(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    """Yahoo indices (^GSPC, ^VIX) sometimes have `previous_close=None` in
    fast_info; we must fall back to history()[Close].iloc[-2]."""
    from mib.sources import yfinance_source as mod

    # Fake fast_info with previous_close=None — our production path simulates
    # a Yahoo index quote.
    class _FakeFastInfo:
        last_price = 7128.92
        previous_close = None
        currency = "USD"
        exchange = "SNP"

    class _FakeTicker:
        def __init__(self, _ticker: str) -> None:
            self.fast_info = _FakeFastInfo()

        def history(self, period: str, interval: str, auto_adjust: bool) -> pd.DataFrame:  # noqa: ARG002
            # Two daily closes: yesterday 7136.76, today (placeholder).
            return pd.DataFrame({"Close": [7120.10, 7136.76, 7128.92]})

    monkeypatch.setattr(mod, "yf", type("_YF", (), {"Ticker": _FakeTicker}))

    src = mod.YFinanceSource()
    quote = await src.fetch_quote("^GSPC")

    # Fallback should have kicked in: change_pct = (7128.92 - 7136.76) / 7136.76 * 100 ≈ -0.11 %
    assert quote.change_24h_pct is not None
    assert quote.change_24h_pct == pytest.approx(
        (7128.92 - 7136.76) / 7136.76 * 100.0, rel=1e-3
    )


@pytest.mark.asyncio
async def test_yf_fetch_quote_falls_back_gracefully_on_history_failure(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    """If history() explodes, fallback must swallow it and leave change_pct None."""
    from mib.sources import yfinance_source as mod

    class _FakeFastInfo:
        last_price = 100.0
        previous_close = None
        currency = "USD"
        exchange = "NASDAQ"

    class _FakeTicker:
        def __init__(self, _ticker: str) -> None:
            self.fast_info = _FakeFastInfo()

        def history(self, period: str, interval: str, auto_adjust: bool) -> pd.DataFrame:  # noqa: ARG002
            raise RuntimeError("transient yahoo outage")

    monkeypatch.setattr(mod, "yf", type("_YF", (), {"Ticker": _FakeTicker}))

    src = mod.YFinanceSource()
    quote = await src.fetch_quote("^BOGUS")

    # No previous_close anywhere → None. Must NOT raise.
    assert quote.change_24h_pct is None
    assert quote.price == pytest.approx(100.0)


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
