"""Unit tests for TradingViewTASource — especially the 3s timeout path."""

from __future__ import annotations

import asyncio

import pytest


class _FakeAnalysis:
    def __init__(self, summary: dict[str, object]) -> None:
        self.summary = summary


class _FakeHandler:
    def __init__(self, summary: dict[str, object], *, slow_s: float = 0.0) -> None:
        self._summary = summary
        self._slow_s = slow_s

    def get_analysis(self) -> _FakeAnalysis:
        if self._slow_s:
            import time

            time.sleep(self._slow_s)
        return _FakeAnalysis(self._summary)


@pytest.mark.asyncio
async def test_tv_fetch_rating_happy_path(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    from mib.sources import tradingview_ta as mod

    class _Ctor:
        def __init__(self, **_kw: object) -> None:
            self._impl = _FakeHandler(
                {"RECOMMENDATION": "BUY", "BUY": 8, "SELL": 2, "NEUTRAL": 3}
            )

        def get_analysis(self) -> _FakeAnalysis:
            return self._impl.get_analysis()

    monkeypatch.setattr(mod, "TA_Handler", _Ctor)

    src = mod.TradingViewTASource()
    rating = await src.fetch_rating("BTCUSDT", kind="crypto", exchange="BINANCE")

    assert rating is not None
    assert rating.recommendation == "BUY"
    assert rating.buy == 8
    assert rating.sell == 2
    assert rating.neutral == 3


@pytest.mark.asyncio
async def test_tv_fetch_rating_times_out_at_3_seconds(
    monkeypatch: pytest.MonkeyPatch, fresh_db: None  # noqa: ARG001
) -> None:
    """If TV takes >3s we must return None and NOT raise."""
    from mib.sources import tradingview_ta as mod

    class _Ctor:
        def __init__(self, **_kw: object) -> None:
            self._impl = _FakeHandler(
                {"RECOMMENDATION": "NEUTRAL"}, slow_s=5.0  # way over the 3s budget
            )

        def get_analysis(self) -> _FakeAnalysis:
            return self._impl.get_analysis()

    monkeypatch.setattr(mod, "TA_Handler", _Ctor)
    # Shorten the hard timeout for the test so it doesn't wait 3s in CI.
    monkeypatch.setattr(mod, "_HARD_TIMEOUT_SEC", 0.3)

    src = mod.TradingViewTASource()
    start = asyncio.get_event_loop().time()
    rating = await src.fetch_rating("BTCUSDT", kind="crypto", exchange="BINANCE")
    elapsed = asyncio.get_event_loop().time() - start

    assert rating is None  # soft-fail, enrichment dropped
    assert elapsed < 1.0, f"should have returned fast, took {elapsed:.2f}s"
