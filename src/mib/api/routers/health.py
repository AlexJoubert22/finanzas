"""`/health` router — liveness + degradation signals.

Security note (spec §13): the response MUST NOT leak user tickers, tokens,
or library versions that would aid fingerprinting. Only the app's own
version (constant) is included.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mib import __version__
from mib.db.session import get_session
from mib.logger import logger
from mib.models.health import HealthResponse

router = APIRouter(tags=["health"])

# Monotonic start-time marker; set once at import.
_STARTED_AT = time.monotonic()


async def _db_ok(session: AsyncSession) -> bool:
    """Run a trivial query to prove the DB is reachable."""
    try:
        result = await session.execute(text("SELECT 1"))
        return result.scalar_one() == 1
    except Exception as exc:  # noqa: BLE001 - we want to catch everything and degrade
        logger.warning("health: db probe failed: {}", exc)
        return False


@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    """Return aggregated liveness information.

    In phase 1 this only proves the API+DB stack is alive. Sources and
    AI quota maps stay empty until their subsystems land (phases 2-4).
    """
    db_ok = await _db_ok(session)

    # Aggregate status: in phase 1 we only know about DB, so that's the
    # single failure mode. Later phases will factor sources and AI quotas.
    status = "ok" if db_ok else "down"

    return HealthResponse(
        status=status,
        db_ok=db_ok,
        sources_status={},
        ai_quotas={},
        uptime_seconds=int(time.monotonic() - _STARTED_AT),
        version=__version__,
        timestamp=datetime.now(UTC),
    )
