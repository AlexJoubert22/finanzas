"""Pydantic schemas for the IA-enriched responses.

Separate module to avoid coupling ``models.market`` to AI concerns
(the AI enrichment must be composable and optional).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Sentiment = Literal["bullish", "bearish", "neutral"]

_DISCLAIMER_TEXT = (
    "No proporcionamos consejos financieros ni de inversión. "
    "Solo análisis descriptivo de los datos provistos. "
    "El usuario debe consultar a un profesional cualificado antes de "
    "tomar decisiones de inversión."
)


class AskRequest(BaseModel):
    """Body of ``POST /ask``."""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=3, max_length=500)


class AskResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    plan: dict[str, Any]
    data: dict[str, Any]
    answer: str
    generated_at: datetime
    disclaimer: str = _DISCLAIMER_TEXT


class ScanHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    reason: str
    price: float | None = None
    rsi: float | None = None
    adx: float | None = None
    macd_hist: float | None = None
    ema_20: float | None = None
    ema_50: float | None = None


class ScanResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    preset: str
    tickers_scanned: int
    hits: list[ScanHit]
    summary: str = ""
    generated_at: datetime
    disclaimer: str = _DISCLAIMER_TEXT


class SentimentAnnotation(BaseModel):
    model_config = ConfigDict(frozen=True)

    sentiment: Sentiment
    rationale: str = ""
