"""Unit tests for the high-level AIService helpers."""

from __future__ import annotations

import pytest

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIResponse, AITask
from mib.services.ai_service import AIService, _extract_json


class _FakeRouter:
    def __init__(self, canned: AIResponse) -> None:
        self._canned = canned
        self.last_task: AITask | None = None

    async def complete(self, task: AITask) -> AIResponse:
        self.last_task = task
        return self._canned


def test_extract_json_handles_plain_object() -> None:
    assert _extract_json('{"sentiment":"bullish"}') == {"sentiment": "bullish"}


def test_extract_json_handles_markdown_fence() -> None:
    assert _extract_json('```json\n{"intent":"macro"}\n```') == {"intent": "macro"}


def test_extract_json_handles_text_around() -> None:
    assert _extract_json('Sure! {"sentiment":"neutral","rationale":"x"} thanks') == {
        "sentiment": "neutral",
        "rationale": "x",
    }


def test_extract_json_returns_none_on_invalid() -> None:
    assert _extract_json("") is None
    assert _extract_json("no json here") is None


@pytest.mark.asyncio
async def test_news_sentiment_falls_back_to_neutral_when_router_fails(fresh_db: None) -> None:  # noqa: ARG001
    router = _FakeRouter(AIResponse(success=False, error="x"))
    service = AIService(router=router)  # type: ignore[arg-type]
    sent, rationale = await service.news_sentiment("Something happened")
    assert sent == "neutral"
    assert rationale == ""


@pytest.mark.asyncio
async def test_news_sentiment_parses_bullish(fresh_db: None) -> None:  # noqa: ARG001
    router = _FakeRouter(
        AIResponse(
            success=True,
            content='{"sentiment":"bullish","rationale":"earnings beat"}',
            provider=ProviderId.GROQ,
            model="llama-3.1-8b-instant",
        )
    )
    service = AIService(router=router)  # type: ignore[arg-type]
    sent, rationale = await service.news_sentiment(
        "Apple beats expectations", "Strong Q4"
    )
    assert sent == "bullish"
    assert "earnings" in rationale


@pytest.mark.asyncio
async def test_plan_query_parses_json(fresh_db: None) -> None:  # noqa: ARG001
    plan_json = '{"intent":"symbol","tickers":["AAPL"],"timeframe":"1h","include_news":false,"summary_focus":"AAPL hoy"}'
    router = _FakeRouter(
        AIResponse(
            success=True,
            content=plan_json,
            provider=ProviderId.OPENROUTER,
            model="openai/gpt-oss-120b:free",
        )
    )
    service = AIService(router=router)  # type: ignore[arg-type]
    plan = await service.plan_query("¿cómo está AAPL?")
    assert plan["intent"] == "symbol"
    assert plan["tickers"] == ["AAPL"]
    assert plan["timeframe"] == "1h"
