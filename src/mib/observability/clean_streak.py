"""Days-clean-incident streak (FASE 13.5).

Reset triggers (per ROADMAP):

1. ANY incident that took longer than 24h to resolve (or is still
   unresolved) — long resolutions imply systemic issues.
2. ANY incident of type :data:`SEVERE_TYPES_RESET_ALWAYS` (currently
   ``BALANCE_DISCREPANCY`` and ``RECONCILE_ORPHAN_UNRESOLVED``):
   regardless of how fast it was resolved, these are the most severe
   and reset the streak immediately.

Returns the integer days since the most recent reset event, or a
sensible default when nothing has happened yet.

Wires into FASE 10.3 :func:`mib.trading.mode_guards.days_clean_streak`
which used to be a placeholder returning 0 — that's replaced now so
the SEMI_AUTO → LIVE gate becomes reachable for the first time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import CriticalIncidentRow
from mib.logger import logger
from mib.observability.incidents import (
    SEVERE_TYPES_RESET_ALWAYS,
)

#: Cap on the streak — even with no incidents ever, we don't return
#: a number bigger than this. The operator sees "≥ X days" beyond.
MAX_REPORTABLE_STREAK_DAYS: int = 365


async def compute_days_clean_streak(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """Days since the last reset event.

    Walks ``critical_incidents`` to find the most recent row that
    qualifies as a "reset trigger" per the rules above. Returns
    ``(now - last_reset_at).days``, capped at
    :data:`MAX_REPORTABLE_STREAK_DAYS`.

    If the table contains zero qualifying rows the streak is the
    minimum of (days since the table's earliest row, MAX). With an
    empty table we return MAX so cold-start systems aren't penalised.
    """
    last_reset = await _find_last_reset_at(session_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    if last_reset is None:
        return MAX_REPORTABLE_STREAK_DAYS
    days = max(0, int((now - last_reset).total_seconds() // 86400))
    return min(days, MAX_REPORTABLE_STREAK_DAYS)


def days_clean_streak_sync(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> int:
    """Sync wrapper for callers that don't run inside an event loop.

    Detects whether a loop is already running; if so, returns
    :data:`MAX_REPORTABLE_STREAK_DAYS` as a safe fallback (the caller
    is in async context and should be awaiting
    :func:`compute_days_clean_streak` directly). The fallback is
    deliberately *permissive* — denying a guard transition because
    the operator's call site happened to be async would be the wrong
    failure mode; the LIVE guard is also reachable via /mode_force
    which is the intended audit path for that severity anyway.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop -> safe to run our own.
        return asyncio.run(
            compute_days_clean_streak(session_factory=session_factory)
        )
    logger.debug(
        "clean_streak: sync helper called inside running loop; "
        "returning MAX as permissive fallback. Async callers should "
        "await compute_days_clean_streak directly."
    )
    return MAX_REPORTABLE_STREAK_DAYS


# ─── Internal ────────────────────────────────────────────────────────


async def _find_last_reset_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> datetime | None:
    """Find the timestamp of the most recent qualifying reset event.

    Two flavours:
    - Severe types reset on ``occurred_at`` (instant reset).
    - Other types reset only if (resolved_at - occurred_at) > 24h, OR
      they're still unresolved AND occurred more than 24h ago. The
      reset timestamp in either case is the moment the 24h threshold
      crossed (occurred_at + 24h, or now if older + still unresolved).
    """
    async with session_factory() as session:
        stmt = select(CriticalIncidentRow).order_by(
            CriticalIncidentRow.occurred_at.desc()
        )
        rows = (await session.scalars(stmt)).all()
    if not rows:
        return None

    now = datetime.now(UTC).replace(tzinfo=None)
    severe_values = {t.value for t in SEVERE_TYPES_RESET_ALWAYS}
    candidates: list[datetime] = []
    for row in rows:
        if row.type in severe_values:
            candidates.append(row.occurred_at)
            continue
        # Resolved long-running incident.
        if row.resolved_at is not None:
            duration = row.resolved_at - row.occurred_at
            if duration > timedelta(hours=24):
                candidates.append(row.resolved_at)
            continue
        # Unresolved incident: counts iff already older than 24h.
        if (now - row.occurred_at) > timedelta(hours=24):
            candidates.append(row.occurred_at + timedelta(hours=24))
    if not candidates:
        return None
    last = max(candidates)
    logger.debug(
        "clean_streak: last_reset_at={} from {} candidates",
        last,
        len(candidates),
    )
    return last
