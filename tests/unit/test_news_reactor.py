"""Tests for :class:`NewsReactor` (FASE 11.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mib.ai.models import ProviderId, TaskType
from mib.ai.providers.base import AIResponse, AITask
from mib.db.models import NewsReactionRow
from mib.db.session import async_session_factory
from mib.models.news import NewsItem, NewsResponse
from mib.trading.alerter import NullAlerter
from mib.trading.news_reactor import (
    DEDUPE_WINDOW,
    NewsReactor,
    _extract_sentiment,
    _hash_news,
    _is_strong_sentiment,
    _parse_decision_payload,
)
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import Trade, TradeInputs


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _news(
    headline: str = "Bullish news on BTC",
    url: str | None = "https://x.com/article-1",
    sentiment: str | None = "bullish",
    ticker: str = "BTC/USDT",
) -> NewsItem:
    return NewsItem(
        headline=headline,
        url=url,
        source="test",
        summary="...",
        published_at=datetime.now(UTC),
        ticker=ticker,
        sentiment=sentiment,
    )


# ─── Pure helpers ────────────────────────────────────────────────────


def test_extract_sentiment_string_to_float() -> None:
    assert _extract_sentiment(_news(sentiment="bullish")) == 0.9
    assert _extract_sentiment(_news(sentiment="bearish")) == -0.9
    assert _extract_sentiment(_news(sentiment="neutral")) == 0.0
    assert _extract_sentiment(_news(sentiment=None)) is None


def test_is_strong_sentiment_threshold() -> None:
    bullish = _news(sentiment="bullish")  # 0.9 > 0.7 → strong
    neutral = _news(sentiment="neutral")  # 0.0 → not strong
    none_sent = _news(sentiment=None)
    assert _is_strong_sentiment(bullish, 0.7) is True
    assert _is_strong_sentiment(neutral, 0.7) is False
    assert _is_strong_sentiment(none_sent, 0.7) is False


def test_hash_news_stable_and_url_preferred() -> None:
    a = _news(url="https://x.com/foo", headline="hX")
    b = _news(url="https://x.com/foo", headline="hY")  # same URL → same hash
    c = _news(url=None, headline="hX")  # falls back to headline
    d = _news(url=None, headline="hX")
    assert _hash_news(a) == _hash_news(b)
    assert _hash_news(c) == _hash_news(d)
    assert _hash_news(a) != _hash_news(c)


def test_parse_decision_happy_path() -> None:
    raw = json.dumps({"decision": "close", "justification": "macro shift"})
    parsed = _parse_decision_payload(raw)
    assert parsed == ("close", "macro shift")


def test_parse_decision_strips_markdown_fences() -> None:
    raw = "```json\n" + json.dumps(
        {"decision": "reduce", "justification": "ok"}
    ) + "\n```"
    assert _parse_decision_payload(raw) == ("reduce", "ok")


def test_parse_decision_invalid_returns_none() -> None:
    assert _parse_decision_payload("garbage") is None
    assert _parse_decision_payload(
        json.dumps({"decision": "panic", "justification": "x"})
    ) is None
    assert _parse_decision_payload(
        json.dumps({"decision": "hold", "justification": ""})
    ) is None


# ─── DB-backed end-to-end with stubs ─────────────────────────────────


class _StubRouter:
    """Mock AIRouter — returns canned responses keyed by call order."""

    def __init__(self, responses: list[AIResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[AITask] = []

    async def complete(self, task: AITask) -> AIResponse:
        self.calls.append(task)
        if not self._responses:
            return AIResponse(
                success=False,
                content="",
                provider=None,
                model="",
                error="no scripted response",
            )
        return self._responses.pop(0)


class _StubNewsService:
    def __init__(self, items_per_ticker: dict[str, list[NewsItem]]) -> None:
        self._items = items_per_ticker

    async def for_ticker(
        self, ticker: str, *, limit: int = 5  # noqa: ARG002
    ) -> NewsResponse:
        return NewsResponse(
            ticker=ticker,
            items=self._items.get(ticker, []),
            generated_at=datetime.now(UTC),
        )


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=63_000.0,
        target_2=66_000.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


async def _seed_open_trade() -> Trade:
    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    persisted = await sig_repo.add(_signal())
    trade = await trade_repo.add(
        TradeInputs(
            signal_id=persisted.id,
            ticker="BTC/USDT",
            side="long",
            size=Decimal("0.001"),
            entry_price=Decimal("60000"),
            stop_loss_price=Decimal("58800"),
            exchange_id="binance_sandbox",
        )
    )
    await trade_repo.transition(
        trade.trade_id,
        "open",
        actor="seed",
        event_type="opened",
        expected_from_status="pending",
    )
    refreshed = await trade_repo.get(trade.trade_id)
    assert refreshed is not None
    return refreshed


def _build_reactor(
    *, news_items: list[NewsItem], router_responses: list[AIResponse]
) -> tuple[NewsReactor, _StubRouter, NullAlerter]:
    router = _StubRouter(router_responses)
    news = _StubNewsService({"BTC/USDT": news_items})
    alerter = NullAlerter()
    reactor = NewsReactor(
        ai_router=router,  # type: ignore[arg-type]
        news_service=news,  # type: ignore[arg-type]
        trade_repo=TradeRepository(async_session_factory),
        session_factory=async_session_factory,
        alerter=alerter,
    )
    return reactor, router, alerter


@pytest.mark.asyncio
async def test_no_open_trades_no_proposals(
    fresh_db: None,  # noqa: ARG001
) -> None:
    reactor, router, alerter = _build_reactor(
        news_items=[_news()],
        router_responses=[],
    )
    proposals = await reactor.run_once()
    assert proposals == []
    assert router.calls == []  # no LLM call without open trades
    assert alerter.recorded == []


@pytest.mark.asyncio
async def test_strong_sentiment_triggers_proposal_persisted_and_alerted(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_open_trade()
    reactor, router, alerter = _build_reactor(
        news_items=[_news(sentiment="bearish", headline="major hack")],
        router_responses=[
            AIResponse(
                success=True,
                content=json.dumps(
                    {
                        "decision": "close",
                        "justification": "negative shock contradicts long",
                    }
                ),
                provider=ProviderId.GROQ,
                model="llama-8b",
                latency_ms=20,
            )
        ],
    )
    proposals = await reactor.run_once()
    assert len(proposals) == 1
    p = proposals[0]
    assert p.decision == "close"
    assert p.ticker == "BTC/USDT"
    assert p.provider_used == "groq"
    assert p.position_trade_id is not None
    # LLM was called exactly once with TaskType.FAST_CLASSIFY.
    assert len(router.calls) == 1
    assert router.calls[0].task_type == TaskType.FAST_CLASSIFY
    # Telegram alert fired.
    assert len(alerter.recorded) == 1
    assert "close" in alerter.recorded[0]
    # Persisted in news_reactions.
    async with async_session_factory() as session:
        from sqlalchemy import select  # noqa: PLC0415

        rows = (
            await session.scalars(select(NewsReactionRow))
        ).all()
        assert len(rows) == 1
        assert rows[0].decision == "close"
        assert rows[0].ai_provider_used == "groq"


@pytest.mark.asyncio
async def test_weak_sentiment_skipped(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_open_trade()
    # neutral sentiment → no LLM call, no proposal.
    reactor, router, alerter = _build_reactor(
        news_items=[_news(sentiment="neutral")],
        router_responses=[],
    )
    proposals = await reactor.run_once()
    assert proposals == []
    assert router.calls == []
    assert alerter.recorded == []


@pytest.mark.asyncio
async def test_dedupe_within_window(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Same news headline + ticker proposed twice within 30 min →
    second run should NOT fire the LLM and should NOT persist again.
    """
    await _seed_open_trade()
    item = _news(sentiment="bearish", headline="hack X", url="https://x.com/h1")
    reactor, router, alerter = _build_reactor(
        news_items=[item],
        router_responses=[
            AIResponse(
                success=True,
                content=json.dumps(
                    {"decision": "close", "justification": "shock"}
                ),
                provider=ProviderId.GROQ,
                model="llama-8b",
                latency_ms=20,
            )
        ],
    )
    first = await reactor.run_once()
    assert len(first) == 1

    # Second run: same news, no new router responses scripted.
    second = await reactor.run_once()
    assert second == []
    # Router was only invoked on the first run.
    assert len(router.calls) == 1
    # Alerter only fired once.
    assert len(alerter.recorded) == 1
    # Only one persisted row.
    async with async_session_factory() as session:
        from sqlalchemy import select  # noqa: PLC0415

        rows = (await session.scalars(select(NewsReactionRow))).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_dedupe_window_expires_after_30min(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """An old persisted reaction (>30 min ago) does NOT block a new one."""
    await _seed_open_trade()
    # Manually seed an OLD reaction row outside the dedupe window.
    old = _now() - DEDUPE_WINDOW - timedelta(minutes=5)
    item = _news(sentiment="bearish", headline="hack X", url="https://x.com/h1")
    url_hash = _hash_news(item)
    async with async_session_factory() as session, session.begin():
        session.add(
            NewsReactionRow(
                news_url_hash=url_hash,
                news_headline=item.headline,
                news_sentiment=-0.9,
                ticker="BTC/USDT",
                position_trade_id=None,
                decision="close",
                justification="historical",
                ai_provider_used="groq",
                ai_model_used="llama",
                decided_at=old,
            )
        )

    reactor, router, _alerter = _build_reactor(
        news_items=[item],
        router_responses=[
            AIResponse(
                success=True,
                content=json.dumps(
                    {"decision": "reduce", "justification": "again"}
                ),
                provider=ProviderId.GROQ,
                model="llama-8b",
                latency_ms=10,
            )
        ],
    )
    proposals = await reactor.run_once()
    assert len(proposals) == 1
    assert proposals[0].decision == "reduce"


@pytest.mark.asyncio
async def test_router_failure_does_not_persist(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_open_trade()
    reactor, _router, alerter = _build_reactor(
        news_items=[_news(sentiment="bearish")],
        router_responses=[
            AIResponse(
                success=False,
                content="",
                provider=None,
                model="",
                error="all providers exhausted",
            )
        ],
    )
    proposals = await reactor.run_once()
    assert proposals == []
    assert alerter.recorded == []
    async with async_session_factory() as session:
        from sqlalchemy import select  # noqa: PLC0415

        rows = (await session.scalars(select(NewsReactionRow))).all()
        assert rows == []


# Suppress unused-import warning for Any.
_ = Any
