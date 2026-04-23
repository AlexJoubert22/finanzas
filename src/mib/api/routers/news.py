"""`GET /news/{ticker}` — headlines for a ticker (no sentiment yet — phase 4)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from mib.api.dependencies import get_ai_service, get_news_service
from mib.logger import logger
from mib.models.news import NewsItem, NewsResponse
from mib.services.ai_service import AIService
from mib.services.news import NewsService

router = APIRouter(tags=["news"])


@router.get("/news/{ticker}", response_model=NewsResponse)
async def get_news_for_ticker(
    ticker: str,
    limit: int = Query(default=10, ge=1, le=30),
    with_sentiment: bool = Query(
        default=True,
        description="If true, classify each headline as bullish/bearish/neutral with IA.",
    ),
    service: NewsService = Depends(get_news_service),
    ai: AIService = Depends(get_ai_service),
) -> NewsResponse:
    """Last ``limit`` headlines for ``ticker`` from Finnhub (fallback RSS)."""
    try:
        resp = await service.for_ticker(ticker, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /news/{} failed: {}", ticker, exc)
        raise HTTPException(
            status_code=502, detail=f"No se pudieron obtener noticias para '{ticker}'."
        ) from exc
    if with_sentiment and resp.items:
        resp = await _attach_sentiment(resp, ai)
    return resp


@router.get("/news", response_model=NewsResponse)
async def get_market_news(
    limit: int = Query(default=15, ge=1, le=50),
    with_sentiment: bool = Query(
        default=False,
        description="If true, run sentiment on each headline. Off by default (cost-heavy).",
    ),
    service: NewsService = Depends(get_news_service),
    ai: AIService = Depends(get_ai_service),
) -> NewsResponse:
    """General market news — Finnhub ``/news?category=general`` or RSS fallback."""
    try:
        resp = await service.market_stream(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /news failed: {}", exc)
        raise HTTPException(status_code=502, detail="No se pudo obtener el stream de noticias.") from exc
    if with_sentiment and resp.items:
        resp = await _attach_sentiment(resp, ai)
    return resp


async def _attach_sentiment(resp: NewsResponse, ai: AIService) -> NewsResponse:
    """Classify each item's headline/summary. Bounded concurrency to keep latency sane."""
    sem = asyncio.Semaphore(4)

    async def classify(item: NewsItem) -> NewsItem:
        async with sem:
            sentiment, rationale = await ai.news_sentiment(item.headline, item.summary)
        return item.model_copy(update={"sentiment": sentiment, "sentiment_rationale": rationale})

    enriched = await asyncio.gather(*(classify(it) for it in resp.items))
    return resp.model_copy(update={"items": enriched})
