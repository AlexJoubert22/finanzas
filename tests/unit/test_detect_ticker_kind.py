"""Unit tests for the ticker-type heuristic."""

from __future__ import annotations

import pytest

from mib.services.market import detect_ticker_kind, normalise_crypto_symbol


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        # Crypto — slash or dash + recognised quote
        ("BTC/USDT", "crypto"),
        ("btc/usdt", "crypto"),  # lowercase gets normalised
        ("BTC-USDT", "crypto"),
        ("ETH/USDC", "crypto"),
        ("SOL-BTC", "crypto"),
        ("LTC/ETH", "crypto"),
        ("BTC-EUR", "crypto"),
        ("ETH-USD", "crypto"),
        # Stocks / ETFs — plain alphanumeric
        ("AAPL", "stock"),
        ("SPY", "stock"),
        ("NVDA", "stock"),
        ("QQQ", "stock"),
        # Share-class suffixes — dash but non-crypto quote
        ("BRK-B", "stock"),
        # Yahoo indices with caret prefix
        ("^GSPC", "stock"),
        ("^VIX", "stock"),
        ("^TNX", "stock"),
        # Forex / futures
        ("EURUSD=X", "stock"),
        ("USDJPY=X", "stock"),
        ("GC=F", "stock"),
        ("CL=F", "stock"),
    ],
)
def test_detect_ticker_kind(ticker: str, expected: str) -> None:
    assert detect_ticker_kind(ticker) == expected


def test_normalise_crypto_symbol() -> None:
    assert normalise_crypto_symbol("btc-usdt") == "BTC/USDT"
    assert normalise_crypto_symbol("BTC/USDT") == "BTC/USDT"
    assert normalise_crypto_symbol("  eth/usdc  ") == "ETH/USDC"
