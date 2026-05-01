"""Append-only repository for ``ai_validations`` (FASE 11.5).

INSERT-only by contract. The coordinator (mib.trading.notify) writes
one row per TRADE_VALIDATE call, regardless of whether the LLM
approved or rejected the signal. Reads serve diagnostics
(/health-style provider breakdowns) and the optional MinAIConfidence
gate (FASE 11.6).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.ai.models import TaskType
from mib.db.models import AIValidationRow
from mib.logger import logger
from mib.trading.ai_validator import AIValidationResult


def derive_request_hash(signal_id: int, prompt_seed: str) -> str:
    """Stable short hash for the request — used for dedup / debugging."""
    h = hashlib.sha256(
        f"{signal_id}|{prompt_seed}".encode()
    ).hexdigest()
    return h[:16]


class AIValidationRepository:
    """INSERT-only persistence for the ``ai_validations`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(
        self,
        *,
        signal_id: int,
        task_type: TaskType,
        result: AIValidationResult,
        request_hash: str,
        decided_at: datetime,
    ) -> int:
        """Persist one validation result. Returns the new pk."""
        async with self._sf() as session, session.begin():
            row = AIValidationRow(
                signal_id=signal_id,
                task_type=task_type.value,
                provider_used=result.provider_used or None,
                model_used=result.model_used or None,
                request_hash=request_hash,
                response_json=_safe_json(result.raw_response),
                approve=result.approve,
                confidence=result.confidence,
                latency_ms=result.latency_ms or None,
                success=result.success,
                error_message=result.error,
                decided_at=decided_at,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
        logger.debug(
            "ai_validations: added id={} signal_id={} provider={} success={}",
            new_id,
            signal_id,
            result.provider_used,
            result.success,
        )
        return new_id

    async def latest_for_signal(
        self, signal_id: int
    ) -> AIValidationRow | None:
        """Most recent validation row for a signal (None if missing)."""
        async with self._sf() as session:
            stmt = (
                select(AIValidationRow)
                .where(AIValidationRow.signal_id == signal_id)
                .order_by(AIValidationRow.decided_at.desc())
                .limit(1)
            )
            return (await session.scalars(stmt)).first()

    async def list_recent_for_provider(
        self, provider_used: str, *, limit: int = 50
    ) -> list[AIValidationRow]:
        async with self._sf() as session:
            stmt = (
                select(AIValidationRow)
                .where(AIValidationRow.provider_used == provider_used)
                .order_by(AIValidationRow.decided_at.desc())
                .limit(limit)
            )
            return list((await session.scalars(stmt)).all())


# ─── Helpers ─────────────────────────────────────────────────────────


def _safe_json(raw: str) -> dict[str, str] | None:
    """Persist the raw provider response as JSON when possible.

    The raw text is stored as ``{"raw": "<content>"}`` if it isn't a
    JSON object so the column type stays consistent. Caller can opt
    to omit the field by passing an empty string.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw[:8000]}
    if isinstance(parsed, dict):
        return parsed
    return {"raw": raw[:8000]}
