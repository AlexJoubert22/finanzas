"""Pydantic schema for the `/health` endpoint response.

The shape matches spec §11bis so external monitors (uptime-kuma, etc.)
can parse it predictably. In phase 1 the `sources_status` and
`ai_quotas` maps are empty — they get populated as those subsystems
come online in later phases.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Top-level health payload."""

    status: Literal["ok", "degraded", "down"] = "ok"
    db_ok: bool = Field(
        description="True if a trivial query against the SQLite DB succeeded.",
    )
    sources_status: dict[str, Literal["ok", "degraded", "down", "not_configured"]] = Field(
        default_factory=dict,
        description="Per-source liveness map; populated in phase 2+.",
    )
    ai_quotas: dict[str, float] = Field(
        default_factory=dict,
        description="Per-provider daily-quota usage 0.0–1.0; populated in phase 4.",
    )
    uptime_seconds: int = Field(ge=0, description="Seconds since app startup.")
    version: str
    timestamp: datetime
