"""Reconciler-failure supervisor (FASE 13.3).

The reconciler itself never raises — its bad runs land in the report
with ``status='error'``. This supervisor watches the success/failure
stream and emits :class:`CriticalIncidentType.RECONCILE_FAILED_PROLONGED`
when the failure window exceeds 30 minutes (configurable threshold).

State is in-memory: the last successful reconcile timestamp + whether
we've already emitted for the current "broken streak". Resetting on
the next successful run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from mib.observability.emitter import IncidentEmitter

PROLONGED_FAILURE_THRESHOLD: timedelta = timedelta(minutes=30)


@dataclass
class ReconcileFailureSupervisor:
    """Tracks consecutive reconcile failures + emits when threshold passed.

    Call :meth:`record_run(success)` after every reconcile_job tick.
    The first failed run starts the broken streak; emission is
    deferred until ``threshold`` elapses. The emission is one-shot
    per streak — a successful run resets it.
    """

    threshold: timedelta = field(default=PROLONGED_FAILURE_THRESHOLD)
    last_success_at: datetime | None = None
    streak_started_at: datetime | None = None
    emitted_for_streak: bool = False

    async def record_run(
        self,
        *,
        success: bool,
        emitter: IncidentEmitter | None,
        now: datetime | None = None,
    ) -> bool:
        """Returns True iff an incident was emitted this call."""
        ts = now or datetime.now(UTC).replace(tzinfo=None)
        if success:
            self.last_success_at = ts
            self.streak_started_at = None
            self.emitted_for_streak = False
            return False

        # Failure path.
        if self.streak_started_at is None:
            self.streak_started_at = ts
            return False

        elapsed = ts - self.streak_started_at
        if elapsed < self.threshold or self.emitted_for_streak:
            return False
        if emitter is None:
            return False

        from mib.observability.incidents import (  # noqa: PLC0415
            CriticalIncidentType,
        )

        try:
            await emitter.emit(
                type_=CriticalIncidentType.RECONCILE_FAILED_PROLONGED,
                context={
                    "streak_started_at": self.streak_started_at.isoformat(),
                    "elapsed_seconds": int(elapsed.total_seconds()),
                },
                severity="critical",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile_supervisor: emit failed: {}", exc
            )
            return False
        self.emitted_for_streak = True
        return True


_supervisor: ReconcileFailureSupervisor | None = None


def get_reconcile_supervisor() -> ReconcileFailureSupervisor:
    """Process-wide singleton (in-memory state)."""
    global _supervisor  # noqa: PLW0603
    if _supervisor is None:
        _supervisor = ReconcileFailureSupervisor()
    return _supervisor


def _reset_for_tests() -> None:
    global _supervisor  # noqa: PLW0603
    _supervisor = None
