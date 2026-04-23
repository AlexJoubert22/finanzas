"""Pydantic schema for the ``/macro`` endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MacroKPI(BaseModel):
    """One macro KPI: label, current value, %change vs previous close, source."""

    model_config = ConfigDict(frozen=True)

    label: str
    ticker: str
    value: float | None = None
    change_pct: float | None = None
    unit: str = ""
    source: str
    as_of: datetime | None = None


class MacroResponse(BaseModel):
    """Aggregated macro snapshot served by ``GET /macro``."""

    model_config = ConfigDict(frozen=True)

    spx: MacroKPI = Field(description="S&P 500 — ^GSPC")
    vix: MacroKPI = Field(description="Volatility Index — ^VIX")
    dxy: MacroKPI = Field(description="Dollar Index — FRED DTWEXBGS (broad TWEX)")
    yield_10y: MacroKPI = Field(description="US 10-Year Treasury yield")
    btc_dominance: MacroKPI = Field(description="BTC dominance vs total crypto market cap")
    generated_at: datetime
    disclaimer: str = (
        "No proporcionamos consejos financieros ni de inversión. "
        "Solo análisis descriptivo de los datos provistos. "
        "El usuario debe consultar a un profesional cualificado antes de "
        "tomar decisiones de inversión."
    )
