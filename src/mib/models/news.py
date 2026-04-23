"""Pydantic schemas for news responses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NewsItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    headline: str
    url: str | None = None
    source: str
    summary: str = ""
    published_at: datetime
    ticker: str | None = None
    sentiment: str | None = Field(
        default=None,
        description="'bullish' | 'bearish' | 'neutral' — populated when IA is available.",
    )
    sentiment_rationale: str | None = None


class NewsResponse(BaseModel):
    """Envelope with disclaimer + news items.

    Sentiment landing in phase 4 will extend this with a per-item field
    ``sentiment: "bullish" | "bearish" | "neutral"`` — for now it's just
    the raw headlines.
    """

    model_config = ConfigDict(frozen=True)

    ticker: str | None = None
    items: list[NewsItem] = Field(default_factory=list)
    generated_at: datetime
    disclaimer: str = (
        "No proporcionamos consejos financieros ni de inversión. "
        "Solo análisis descriptivo de los datos provistos. "
        "El usuario debe consultar a un profesional cualificado antes de "
        "tomar decisiones de inversión."
    )
