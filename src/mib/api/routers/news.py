"""`GET /news/{ticker}` — headlines for a ticker (no sentiment yet — phase 4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from mib.api.dependencies import get_news_service
from mib.logger import logger
from mib.models.news import NewsResponse
from mib.services.news import NewsService

router = APIRouter(tags=["news"])


@router.get("/news/{ticker}", response_model=NewsResponse)
async def get_news_for_ticker(
    ticker: str,
    limit: int = Query(default=10, ge=1, le=30),
    service: NewsService = Depends(get_news_service),
) -> NewsResponse:
    """Last ``limit`` headlines for ``ticker`` from Finnhub (fallback RSS)."""
    try:
        return await service.for_ticker(ticker, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /news/{} failed: {}", ticker, exc)
        raise HTTPException(
            status_code=502, detail=f"No se pudieron obtener noticias para '{ticker}'."
        ) from exc


@router.get("/news", response_model=NewsResponse)
async def get_market_news(
    limit: int = Query(default=15, ge=1, le=50),
    service: NewsService = Depends(get_news_service),
) -> NewsResponse:
    """General market news — Finnhub ``/news?category=general`` or RSS fallback."""
    try:
        return await service.market_stream(limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET /news failed: {}", exc)
        raise HTTPException(status_code=502, detail="No se pudo obtener el stream de noticias.") from exc
