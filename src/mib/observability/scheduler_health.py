"""Scheduler health tracker (FASE 13.7).

Each periodic job updates :class:`SchedulerHealth` after a successful
tick. The /heartbeat endpoint reads ``last_tick_at`` to decide whether
the bot is still alive — if no job ticked in the last 60 seconds
something is wrong (the scheduler stalled, the process is wedged, the
event loop is starved).

Same pattern for the reconciler: ``last_reconcile_at`` flags whether
the 5-min loop is still running. >10 min without a successful
reconcile flips heartbeat to 503 because reconciliation is the
canonical safety net.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class SchedulerHealth:
    """In-memory snapshot of scheduler liveness."""

    last_tick_at: datetime | None = field(default=None)
    last_reconcile_at: datetime | None = field(default=None)

    def mark_tick(self) -> None:
        self.last_tick_at = datetime.now(UTC).replace(tzinfo=None)

    def mark_reconcile(self) -> None:
        self.last_reconcile_at = datetime.now(UTC).replace(tzinfo=None)


_health: SchedulerHealth | None = None


def get_scheduler_health() -> SchedulerHealth:
    global _health  # noqa: PLW0603
    if _health is None:
        _health = SchedulerHealth()
    return _health


def _reset_for_tests() -> None:
    global _health  # noqa: PLW0603
    _health = None
