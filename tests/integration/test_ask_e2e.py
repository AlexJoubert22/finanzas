"""End-to-end tests for the /ask endpoint using FastAPI TestClient.

We stub AIRouter and each Service dependency so the test exercises the
whole FastAPI stack (dependency injection, validation, serialisation)
without hitting any upstream API.

Three scenarios cover the spec's three canonical paths:

    (i)   cripto query  — intent=symbol tickers=["BTC/USDT"]
    (ii)  macro query   — intent=macro   tickers=[]
    (iii) graceful degradation — a data source raises → response still 200
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mib.api.app import create_app
from mib.api.dependencies import (
    get_ai_service,
    get_macro_service,
    get_market_service,
    get_news_service,
)
from mib.models.macro import MacroKPI, MacroResponse
from mib.models.market import Candle, Quote, SymbolResponse

# ─── Fakes ────────────────────────────────────────────────────────────


class _FakeAIService:
    """Minimal AIService stub — returns a pre-set plan and canned answer."""

    def __init__(self, plan: dict[str, Any], answer: str = "Resumen fake.") -> None:
        self._plan = plan
        self._answer = answer
        self.plan_calls = 0
        self.summarise_calls = 0
        self.last_collected: dict[str, Any] | None = None

    async def plan_query(self, question: str) -> dict[str, Any]:
        self.plan_calls += 1
        return self._plan

    async def summarise_answer(
        self, question: str, plan: dict[str, Any], collected: dict[str, Any]
    ) -> str:
        self.summarise_calls += 1
        self.last_collected = collected
        return self._answer


class _FakeMarketService:
    def __init__(self, quote_price: float = 77500.0, fail: bool = False) -> None:
        self._price = quote_price
        self._fail = fail

    async def get_symbol(
        self, ticker: str, *, ohlcv_timeframe: str, ohlcv_limit: int
    ) -> SymbolResponse:
        if self._fail:
            raise RuntimeError("market upstream down")
        return SymbolResponse(
            quote=Quote(
                ticker=ticker,
                kind="crypto",
                source="ccxt:binance",
                price=self._price,
                change_24h_pct=-1.1,
                currency="USDT",
                venue="binance",
                timestamp=datetime.now(UTC),
            ),
            candles=[
                Candle(
                    timestamp=datetime.now(UTC),
                    open=self._price * 0.99,
                    high=self._price * 1.01,
                    low=self._price * 0.98,
                    close=self._price,
                    volume=1000.0,
                )
            ],
        )


class _FakeMacroService:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    async def snapshot(self) -> MacroResponse:
        if self._fail:
            raise RuntimeError("fred down")
        return MacroResponse(
            spx=MacroKPI(label="S&P 500", ticker="^GSPC", value=7128.92, change_pct=-0.28, source="yfinance"),
            vix=MacroKPI(label="VIX", ticker="^VIX", value=19.12, change_pct=+1.64, source="yfinance"),
            dxy=MacroKPI(label="USD Index", ticker="DTWEXBGS", value=118.08, source="fred"),
            yield_10y=MacroKPI(label="10Y", ticker="DGS10", value=4.30, unit="%", source="fred"),
            btc_dominance=MacroKPI(label="BTC.D", ticker="BTC.D", value=58.15, unit="%", source="coingecko"),
            generated_at=datetime.now(UTC),
        )


class _FakeNewsService:
    async def for_ticker(self, ticker: str, *, limit: int) -> Any:  # pragma: no cover - not hit in these tests
        from mib.models.news import NewsResponse

        return NewsResponse(ticker=ticker, items=[], generated_at=datetime.now(UTC))

    async def market_stream(self, limit: int) -> Any:  # pragma: no cover
        from mib.models.news import NewsResponse

        return NewsResponse(ticker=None, items=[], generated_at=datetime.now(UTC))


# ─── Helpers to wire fakes into the FastAPI app ───────────────────────


def _client_with(
    ai: _FakeAIService,
    market: _FakeMarketService,
    macro: _FakeMacroService,
    news: _FakeNewsService | None = None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_ai_service] = lambda: ai
    app.dependency_overrides[get_market_service] = lambda: market
    app.dependency_overrides[get_macro_service] = lambda: macro
    app.dependency_overrides[get_news_service] = lambda: news or _FakeNewsService()
    return TestClient(app)


# ─── Tests ────────────────────────────────────────────────────────────


def test_ask_crypto_query_happy_path(fresh_db: None) -> None:  # noqa: ARG001
    """(i) cripto query: intent=symbol tickers=['BTC/USDT']."""
    ai = _FakeAIService(
        plan={
            "intent": "symbol",
            "tickers": ["BTC/USDT"],
            "timeframe": "1h",
            "include_news": False,
        },
        answer="BTC cotiza a 77500 con RSI neutro.",
    )
    market = _FakeMarketService(quote_price=77500.0)
    client = _client_with(ai, market, _FakeMacroService())

    resp = client.post("/ask", json={"question": "¿cómo está BTC?"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["plan"]["intent"] == "symbol"
    assert "BTC/USDT" in body["plan"]["tickers"]
    assert "symbols" in body["data"]
    assert body["data"]["symbols"]["BTC/USDT"]["quote"]["price"] == pytest.approx(77500.0)
    assert body["answer"] == "BTC cotiza a 77500 con RSI neutro."
    assert "No proporcionamos consejos" in body["disclaimer"]
    assert ai.plan_calls == 1
    assert ai.summarise_calls == 1


def test_ask_macro_query_happy_path(fresh_db: None) -> None:  # noqa: ARG001
    """(ii) macro query: intent=macro, sin tickers."""
    ai = _FakeAIService(
        plan={"intent": "macro", "tickers": [], "timeframe": "1h", "include_news": False},
        answer="El mercado cierra mixto: SPX -0.28%, VIX +1.64%.",
    )
    client = _client_with(ai, _FakeMarketService(), _FakeMacroService())

    resp = client.post("/ask", json={"question": "¿cómo está el mercado hoy?"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["plan"]["intent"] == "macro"
    assert "macro" in body["data"]
    assert body["data"]["macro"]["spx"]["value"] == pytest.approx(7128.92)
    assert "symbols" not in body["data"]  # no tickers en el plan
    assert "SPX" in body["answer"]


def test_ask_graceful_degradation_when_source_fails(fresh_db: None) -> None:  # noqa: ARG001
    """(iii) una fuente cae → el endpoint sigue devolviendo 200 con los datos que sí haya."""
    ai = _FakeAIService(
        plan={
            "intent": "compare",  # compare pulls macro too
            "tickers": ["BTC/USDT"],
            "timeframe": "1h",
            "include_news": False,
        },
        answer="BTC bajó pero no disponemos de datos macro ahora.",
    )
    # Macro falla, mercado responde: esperamos que el endpoint siga 200.
    market = _FakeMarketService(quote_price=77500.0)
    macro = _FakeMacroService(fail=True)
    client = _client_with(ai, market, macro)

    resp = client.post("/ask", json={"question": "compara BTC con el mercado"})
    assert resp.status_code == 200
    body = resp.json()
    assert "symbols" in body["data"]
    # macro debería estar ausente (collection failed silently) pero el cuerpo sigue válido
    assert "macro" not in body["data"]
    assert body["answer"]  # no vacío
