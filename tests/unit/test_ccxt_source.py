"""Unit tests for CCXTSource using a monkeypatched ccxt exchange client."""

from __future__ import annotations

from typing import Any

import pytest


class _FakeExchange:
    """Mimics the subset of `ccxt.async_support.Exchange` we use."""

    def __init__(self) -> None:
        self.fetch_ticker_calls: list[str] = []
        self.fetch_ohlcv_calls: list[tuple[str, str, int]] = []

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self.fetch_ticker_calls.append(symbol)
        return {
            "symbol": symbol,
            "last": 98432.5,
            "percentage": 2.34,
            "timestamp": 1_747_000_000_000,
            "quoteVolume": 12345.0,
        }

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> list[list[float]]:
        self.fetch_ohlcv_calls.append((symbol, timeframe, limit))
        # Two bars: [ts_ms, open, high, low, close, volume]
        return [
            [1_747_000_000_000, 100.0, 105.0, 99.0, 103.0, 1_000.0],
            [1_747_003_600_000, 103.0, 110.0, 101.0, 108.0, 1_500.0],
        ]

    async def close(self) -> None:
        pass


@pytest.fixture()
def fake_ccxt(monkeypatch: pytest.MonkeyPatch) -> _FakeExchange:
    fake = _FakeExchange()

    def _patched_exchange(self: object) -> _FakeExchange:  # noqa: ARG001
        return fake

    from mib.sources import ccxt_source

    monkeypatch.setattr(ccxt_source.CCXTSource, "_get_exchange", _patched_exchange)
    return fake


@pytest.mark.asyncio
async def test_ccxt_fetch_quote_maps_fields(
    fake_ccxt: _FakeExchange, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources.ccxt_source import CCXTSource

    src = CCXTSource(exchange_id="binance")
    quote = await src.fetch_quote("BTC/USDT")

    assert quote.ticker == "BTC/USDT"
    assert quote.kind == "crypto"
    assert quote.source == "ccxt:binance"
    assert quote.price == pytest.approx(98432.5)
    assert quote.change_24h_pct == pytest.approx(2.34)
    assert quote.currency == "USDT"
    assert quote.venue == "binance"
    assert fake_ccxt.fetch_ticker_calls == ["BTC/USDT"]


@pytest.mark.asyncio
async def test_ccxt_fetch_ohlcv_returns_candles(
    fake_ccxt: _FakeExchange, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources.ccxt_source import CCXTSource

    src = CCXTSource(exchange_id="binance")
    candles = await src.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=2)

    assert len(candles) == 2
    assert candles[0].open == 100.0
    assert candles[0].close == 103.0
    assert candles[1].high == 110.0
    assert fake_ccxt.fetch_ohlcv_calls == [("BTC/USDT", "1h", 2)]


@pytest.mark.asyncio
async def test_ccxt_second_call_hits_cache(
    fake_ccxt: _FakeExchange, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources.ccxt_source import CCXTSource

    src = CCXTSource(exchange_id="binance")
    await src.fetch_quote("BTC/USDT")
    await src.fetch_quote("BTC/USDT")  # should hit cache
    # Loader went to the exchange only once.
    assert fake_ccxt.fetch_ticker_calls == ["BTC/USDT"]
