"""Circuit-breaker framework placeholder (FASE 13.3 → FASE 24 full).

Real circuit breakers with sliding-window error tracking, half-open
testing, and per-exchange isolation land in FASE 24. FASE 13.3 ships
the **emit path**: a simple Lock + open-since timestamp model that
fires :class:`CriticalIncidentType.CIRCUIT_BREAKER_PROLONGED` once a
breaker has been open for more than :data:`PROLONGED_OPEN_THRESHOLD`
(default 15 minutes).

The class is deliberately tiny — adding state and transitions later
won't break callers that only know the public ``name``, ``open()``,
``close()``, and ``check_prolonged()`` methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from mib.observability.emitter import IncidentEmitter

PROLONGED_OPEN_THRESHOLD: timedelta = timedelta(minutes=15)


@dataclass
class _BreakerState:
    name: str
    opened_at: datetime | None = None
    last_emitted_for: datetime | None = None
    """Timestamp of the ``opened_at`` we already emitted an incident
    for. Prevents re-emitting on every supervisor tick."""


@dataclass
class CircuitBreakerRegistry:
    """In-memory map of breaker name → state.

    Reset by tests; production lives for the duration of the process.
    """

    breakers: dict[str, _BreakerState] = field(default_factory=dict)

    def open(self, name: str) -> None:
        """Mark the named breaker as tripped. Idempotent."""
        b = self.breakers.get(name)
        if b is None:
            self.breakers[name] = _BreakerState(
                name=name,
                opened_at=datetime.now(UTC).replace(tzinfo=None),
            )
        elif b.opened_at is None:
            b.opened_at = datetime.now(UTC).replace(tzinfo=None)
            b.last_emitted_for = None

    def close(self, name: str) -> None:
        """Mark the breaker healthy again. Resets emission tracking."""
        b = self.breakers.get(name)
        if b is None:
            return
        b.opened_at = None
        b.last_emitted_for = None

    def is_open(self, name: str) -> bool:
        b = self.breakers.get(name)
        return b is not None and b.opened_at is not None

    async def check_prolonged(
        self,
        emitter: IncidentEmitter,
        *,
        threshold: timedelta = PROLONGED_OPEN_THRESHOLD,
    ) -> int:
        """Emit CIRCUIT_BREAKER_PROLONGED for any breaker open > threshold.

        Returns the number of incidents emitted this call. Idempotent
        per-incident: re-emits only if a breaker has been re-opened
        since the last emission.
        """
        from mib.observability.incidents import (  # noqa: PLC0415
            CriticalIncidentType,
        )

        now = datetime.now(UTC).replace(tzinfo=None)
        emitted = 0
        for b in self.breakers.values():
            if b.opened_at is None:
                continue
            elapsed = now - b.opened_at
            if elapsed < threshold:
                continue
            # Skip if we already emitted for this same opened_at instance.
            if b.last_emitted_for == b.opened_at:
                continue
            try:
                await emitter.emit(
                    type_=CriticalIncidentType.CIRCUIT_BREAKER_PROLONGED,
                    context={
                        "breaker": b.name,
                        "opened_at": b.opened_at.isoformat(),
                        "elapsed_seconds": int(elapsed.total_seconds()),
                    },
                    severity="critical",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "circuit_breaker: emit failed for {}: {}", b.name, exc
                )
                continue
            b.last_emitted_for = b.opened_at
            emitted += 1
        return emitted


_registry: CircuitBreakerRegistry | None = None


def get_circuit_breakers() -> CircuitBreakerRegistry:
    """Process-wide singleton."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def _reset_for_tests() -> None:
    """Test-only — wipe the singleton."""
    global _registry  # noqa: PLW0603
    _registry = None
