"""Snapshot-style tests for Telegram HTML formatters.

We don't use pytest-regressions: the assertions are small enough to
keep inline, and inline snapshots make intent obvious on failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mib.telegram.formatters import (
    chunk,
    direction_emoji,
    esc,
    fmt_alerts_list,
    fmt_ask_answer,
    fmt_macro_card,
    fmt_news_list,
    fmt_pct,
    fmt_price,
    fmt_price_card,
    fmt_scan_result,
    fmt_status,
    fmt_watch_created,
    fmt_watch_triggered,
    sentiment_emoji,
)


def test_esc_escapes_html_metachars() -> None:
    assert esc("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"


def test_direction_emoji_bands() -> None:
    assert direction_emoji(None) == "⚪"
    assert direction_emoji(0.0) == "⚪"
    assert direction_emoji(0.5) == "🟢"
    assert direction_emoji(-0.5) == "🔴"
    # dead band ±0.1
    assert direction_emoji(0.05) == "⚪"


def test_sentiment_emoji() -> None:
    assert sentiment_emoji("bullish") == "🟢"
    assert sentiment_emoji("bearish") == "🔴"
    assert sentiment_emoji("neutral") == "⚪"
    assert sentiment_emoji(None) == "⚪"


def test_fmt_price_and_pct() -> None:
    assert fmt_price(None) == "—"
    # 1_234.5 → "1 234.50" (space thousand sep)
    assert fmt_price(1234.5, decimals=2) == "1 234.50"
    assert fmt_pct(None) == "—"
    assert fmt_pct(1.234) == "+1.23%"
    assert fmt_pct(-0.5) == "-0.50%"


def test_chunk_short_text_is_single_message() -> None:
    assert chunk("hola") == ["hola"]


def test_chunk_long_text_splits_at_paragraph() -> None:
    body = ("a" * 3000) + "\n\n" + ("b" * 2000)
    parts = chunk(body)
    assert len(parts) >= 2
    assert all(len(p) <= 4000 for p in parts)
    # No paragraph break lost: second chunk starts with the 'b' block
    assert parts[1].startswith("b")


def test_fmt_price_card_contains_key_fields() -> None:
    payload = {
        "quote": {
            "ticker": "BTC/USDT",
            "price": 77500.0,
            "currency": "USDT",
            "venue": "binance",
            "change_24h_pct": -1.23,
            "timestamp": datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
        },
        "indicators": {
            "rsi_14": 42.5,
            "macd": 1.1,
            "macd_signal": 0.8,
            "macd_hist": 0.3,
            "ema_20": 77000.0,
            "ema_50": 76000.0,
            "ema_200": 70000.0,
            "adx_14": 18.4,
        },
        "technical_rating": {
            "recommendation": "NEUTRAL",
            "buy": 5,
            "sell": 4,
            "neutral": 9,
        },
        "ai_analysis": "BTC consolida.",
    }
    out = fmt_price_card(payload)
    assert "<b>BTC/USDT</b>" in out
    assert "RSI(14): 42.5" in out
    assert "ADX(14): 18.4" in out
    assert "TradingView:" in out
    assert "BTC consolida" in out
    assert "No es consejo financiero" in out


def test_fmt_macro_card_lists_five_kpis() -> None:
    payload = {
        "spx": {"value": 7128.92, "change_pct": -0.28},
        "vix": {"value": 19.12, "change_pct": 1.64},
        "dxy": {"value": 118.08},
        "yield_10y": {"value": 4.30, "unit": "%"},
        "btc_dominance": {"value": 58.15, "unit": "%"},
    }
    out = fmt_macro_card(payload)
    for label in ("S&amp;P 500", "VIX", "USD Index", "10Y Treasury", "BTC Dominance"):
        assert label in out


def test_fmt_news_list_empty_state_and_items() -> None:
    empty = fmt_news_list({"ticker": "AAPL", "items": []})
    assert "Sin noticias" in empty

    out = fmt_news_list({
        "ticker": "AAPL",
        "items": [
            {
                "headline": "Apple beats earnings",
                "url": "https://example.com/a",
                "source": "Reuters",
                "sentiment": "bullish",
                "sentiment_rationale": "Record revenue guidance.",
            },
        ],
    })
    assert "🟢" in out
    assert 'href="https://example.com/a"' in out
    assert "Reuters" in out


def test_fmt_scan_result_with_hits_and_summary() -> None:
    out = fmt_scan_result({
        "preset": "oversold",
        "tickers_scanned": 40,
        "hits": [{"ticker": "ETH/USDT", "reason": "RSI=28.1"}],
        "summary": "Un par de cripto sobrevendidas.",
    })
    assert "preset=oversold" in out
    assert "ETH/USDT" in out
    assert "RSI=28.1" in out
    assert "Resumen IA" in out


def test_fmt_alerts_list_empty_and_populated() -> None:
    assert "Sin alertas activas" in fmt_alerts_list([])
    out = fmt_alerts_list([
        {"id": 7, "ticker": "BTC/USDT", "operator": ">", "target_price": 100000.0}
    ])
    assert "#7" in out
    assert "BTC/USDT" in out
    assert "100 000.00" in out


def test_fmt_watch_created_and_triggered() -> None:
    assert "Alerta creada" in fmt_watch_created("ETH/USDT", "<", 2500.0)
    out = fmt_watch_triggered("ETH/USDT", ">", 3000.0, 3050.5)
    assert "Alerta disparada" in out
    assert "🟢" in out  # operator ">"
    assert "3 050.50" in out


def test_fmt_ask_answer_wraps_question_and_answer() -> None:
    out = fmt_ask_answer("¿Cómo está BTC?", "Consolida entre 75k y 80k.")
    assert "¿Cómo está BTC?" in out
    assert "Consolida" in out
    assert "No es consejo financiero" in out


def test_fmt_status_renders_sources_and_quotas() -> None:
    out = fmt_status({
        "status": "ok",
        "uptime_seconds": 3723,  # 1h 02m
        "sources_status": {"ccxt": "ok", "finnhub": "down"},
        "ai_quotas": {"groq": 0.123, "openrouter": 0.9},
    })
    assert "1h 2m" in out
    assert "🟢 ccxt" in out
    assert "🔴 finnhub" in out
    assert "groq: 12.3%" in out
    assert "openrouter: 90.0%" in out
