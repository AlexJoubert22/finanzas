"""Tests for the TradingView exchange resolver."""

from __future__ import annotations

import pytest

from mib.sources.tv_exchange_map import (
    is_forex_or_futures,
    resolve_tv_exchange,
)


@pytest.mark.parametrize(
    ("ticker", "expected_exchange", "expected_symbol"),
    [
        # NASDAQ — tech blue-chips
        ("AAPL", "NASDAQ", "AAPL"),
        ("MSFT", "NASDAQ", "MSFT"),
        ("NVDA", "NASDAQ", "NVDA"),
        ("QQQ", "NASDAQ", "QQQ"),
        # NASDAQ — ADRs (spec condition 3: ASML must resolve correctly)
        ("ASML", "NASDAQ", "ASML"),
        # NYSE — blue-chips + ADRs
        ("JPM", "NYSE", "JPM"),
        ("IBM", "NYSE", "IBM"),
        # NYSE — ADRs (spec condition 3: TSM, SHOP must resolve correctly)
        ("TSM", "NYSE", "TSM"),
        ("SHOP", "NYSE", "SHOP"),
        ("BABA", "NYSE", "BABA"),
        # AMEX / NYSE Arca — ETFs
        ("SPY", "AMEX", "SPY"),
        ("GLD", "AMEX", "GLD"),
        ("VOO", "AMEX", "VOO"),
        # Indices — stripped `^` prefix, routed to INDEX/TVC
        ("^GSPC", "INDEX", "SPX"),
        ("^VIX", "INDEX", "VIX"),
        ("^TNX", "TVC", "US10Y"),
        ("^DXY", "TVC", "DXY"),
        ("^IBEX", "INDEX", "IBEX35"),
        # Fallback — unknown ticker → NASDAQ (soft-fail upstream if wrong)
        ("XYZINVENTED", "NASDAQ", "XYZINVENTED"),
    ],
)
def test_resolve_tv_exchange(
    ticker: str, expected_exchange: str, expected_symbol: str
) -> None:
    ex, sym = resolve_tv_exchange(ticker)
    assert ex == expected_exchange, (
        f"{ticker}: expected exchange={expected_exchange!r}, got {ex!r}"
    )
    assert sym == expected_symbol


def test_resolve_case_insensitive() -> None:
    # Lowercase input still maps correctly (critical for user-supplied URLs).
    assert resolve_tv_exchange("aapl") == ("NASDAQ", "AAPL")
    assert resolve_tv_exchange("tsm") == ("NYSE", "TSM")
    assert resolve_tv_exchange("spy") == ("AMEX", "SPY")


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("EURUSD=X", True),
        ("USDJPY=X", True),
        ("GC=F", True),
        ("CL=F", True),
        ("AAPL", False),
        ("BTC/USDT", False),
        ("^GSPC", False),
    ],
)
def test_is_forex_or_futures(ticker: str, expected: bool) -> None:
    assert is_forex_or_futures(ticker) is expected
