"""Critical incidents domain (FASE 13.2).

Defines the 7-type :class:`CriticalIncidentType` enum locked in by
ROADMAP Apéndice A, the :class:`CriticalIncident` value object, and
:class:`CriticalIncidentRepository` — append-only with TWO controlled
structural updates (resolved_at + resolution_notes via
:meth:`resolve_incident`).

The 7 incident types are exhaustive: any new type requires a
strategic-session decision and a code PR (NOT a config flip), so a
hot-reload can't silently introduce new noise into the operator's
dashboard.

Severity values: ``info`` | ``warning`` | ``critical``. The bot's
auto-emitters default to ``warning``; ``critical`` is reserved for
incidents that should page the operator over Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import CriticalIncidentRow
from mib.logger import logger

Severity = Literal["info", "warning", "critical"]


class CriticalIncidentType(StrEnum):
    """The 7 operational incident types from ROADMAP Apéndice A.

    NEVER add new values without a strategic-session decision — the
    enum is the single source of truth across:
    - the ``critical_incidents.type`` CHECK constraint,
    - the Prometheus ``mib_critical_incident_total{type=...}`` label,
    - the days_clean_streak() reset rules.
    """

    RECONCILE_ORPHAN_UNRESOLVED = "reconcile.orphan_unresolved"
    BALANCE_DISCREPANCY = "reconcile.balance_unattributed"
    CIRCUIT_BREAKER_PROLONGED = "circuit_breaker.open_over_15min"
    NATIVE_STOP_MISSING_AFTER_FILL = "executor.stop_missing_post_fill"
    KILL_SWITCH_DD_DAILY = "risk.kill_switch_daily_dd"
    MANUAL_INTERVENTION_REQUIRED = "ops.manual_intervention"
    RECONCILE_FAILED_PROLONGED = "reconcile.failed_over_30min"


#: Subset of types that ALWAYS reset the clean-streak (regardless of
#: how fast they were resolved). Per FASE 13.5 spec — these are the
#: most severe and resetting on resolution time alone is insufficient.
SEVERE_TYPES_RESET_ALWAYS: frozenset[CriticalIncidentType] = frozenset({
    CriticalIncidentType.BALANCE_DISCREPANCY,
    CriticalIncidentType.RECONCILE_ORPHAN_UNRESOLVED,
})


@dataclass(frozen=True)
class CriticalIncident:
    """In-memory view of a row in ``critical_incidents``."""

    id: int
    type: CriticalIncidentType
    occurred_at: datetime
    resolved_at: datetime | None
    auto_detected: bool
    severity: Severity
    context: dict[str, Any] | None
    resolution_notes: str | None


class IncidentAlreadyResolvedError(Exception):
    """Raised when ``resolve_incident`` is called on an already-resolved row."""

    def __init__(self, incident_id: int) -> None:
        super().__init__(f"incident #{incident_id} already resolved")
        self.incident_id = incident_id


class CriticalIncidentRepository:
    """Append-only persistence boundary for ``critical_incidents``.

    Allowed mutations:
    - ``add()``: INSERT.
    - ``resolve_incident(id, notes)``: set ``resolved_at`` + notes
      ONCE. Raises :class:`IncidentAlreadyResolvedError` on second call.

    Forbidden: any other UPDATE on the table.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ───────────────────────────────────────────────────

    async def add(
        self,
        *,
        type_: CriticalIncidentType,
        occurred_at: datetime,
        auto_detected: bool,
        severity: Severity = "warning",
        context: dict[str, Any] | None = None,
    ) -> int:
        """Append a new incident row. Returns the new pk."""
        async with self._sf() as session, session.begin():
            row = CriticalIncidentRow(
                type=type_.value,
                occurred_at=occurred_at,
                resolved_at=None,
                auto_detected=auto_detected,
                severity=severity,
                context_json=context,
                resolution_notes=None,
            )
            session.add(row)
            await session.flush()
            new_id = int(row.id)
        logger.info(
            "incidents: new id={} type={} severity={} auto={}",
            new_id,
            type_.value,
            severity,
            auto_detected,
        )
        return new_id

    async def resolve_incident(
        self,
        incident_id: int,
        *,
        resolved_at: datetime,
        notes: str,
    ) -> CriticalIncident | None:
        """Mark an incident resolved. Raises if already resolved.

        Returns the updated row, or None if the id doesn't exist.
        """
        async with self._sf() as session, session.begin():
            row = await session.get(CriticalIncidentRow, incident_id)
            if row is None:
                return None
            if row.resolved_at is not None:
                raise IncidentAlreadyResolvedError(incident_id)
            row.resolved_at = resolved_at
            row.resolution_notes = notes
            await session.flush()
            return _to_dc(row)

    # ─── Reads ────────────────────────────────────────────────────

    async def get_by_id(self, incident_id: int) -> CriticalIncident | None:
        async with self._sf() as session:
            row = await session.get(CriticalIncidentRow, incident_id)
            return _to_dc(row) if row is not None else None

    async def list_recent(
        self, *, since: datetime | None = None, limit: int = 100
    ) -> list[CriticalIncident]:
        async with self._sf() as session:
            stmt = select(CriticalIncidentRow).order_by(
                CriticalIncidentRow.occurred_at.desc()
            )
            if since is not None:
                stmt = stmt.where(CriticalIncidentRow.occurred_at >= since)
            stmt = stmt.limit(limit)
            rows = (await session.scalars(stmt)).all()
            return [_to_dc(r) for r in rows]


# ─── Helpers ─────────────────────────────────────────────────────────


def _to_dc(row: CriticalIncidentRow) -> CriticalIncident:
    return CriticalIncident(
        id=int(row.id),
        type=CriticalIncidentType(row.type),
        occurred_at=row.occurred_at,
        resolved_at=row.resolved_at,
        auto_detected=row.auto_detected,
        severity=row.severity,  # type: ignore[arg-type]
        context=dict(row.context_json) if row.context_json is not None else None,
        resolution_notes=row.resolution_notes,
    )
