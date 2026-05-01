"""IncidentEmitter — single helper that bumps DB + Prometheus counter (FASE 13.3).

Every auto-detected incident path goes through :class:`IncidentEmitter`
so the two side effects (DB row + ``mib_critical_incident_total``
metric label) can never drift. Future emitters (FASE 24 circuit
breakers, FASE 28 macro shocks) keep using the same helper — no
direct ``CriticalIncidentRepository.add`` calls outside of unit tests.

The emitter is tolerant: a Prometheus bump failure is logged but
NEVER blocks the DB write. The row is the source of truth; the
gauge is a read-side view.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mib.logger import logger
from mib.observability.incidents import (
    CriticalIncidentRepository,
    CriticalIncidentType,
    Severity,
)
from mib.observability.metrics import get_metrics_registry


class IncidentEmitter:
    """Wraps :class:`CriticalIncidentRepository` + Prometheus bump."""

    def __init__(self, repo: CriticalIncidentRepository) -> None:
        self._repo = repo

    async def emit(
        self,
        *,
        type_: CriticalIncidentType,
        context: dict[str, Any] | None = None,
        severity: Severity = "warning",
        auto_detected: bool = True,
        occurred_at: datetime | None = None,
    ) -> int:
        """Persist + bump the metric. Returns the new incident id."""
        when = occurred_at or datetime.now(UTC).replace(tzinfo=None)
        new_id = await self._repo.add(
            type_=type_,
            occurred_at=when,
            auto_detected=auto_detected,
            severity=severity,
            context=context,
        )
        try:
            reg = get_metrics_registry()
            reg.critical_incident_total.labels(type=type_.value).inc()
        except Exception as exc:  # noqa: BLE001 — metric must never block DB
            logger.warning(
                "incidents: prometheus bump failed for {} (id={}): {}",
                type_.value,
                new_id,
                exc,
            )
        return new_id
