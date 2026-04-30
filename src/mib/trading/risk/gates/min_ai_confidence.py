"""MinAIConfidenceGate — optional opt-in gate (FASE 11.6).

Rejects signals whose ``confidence_ai`` (set by the FASE 11.2
TradeValidator) is below a configured threshold. NOT registered by
default — :func:`api.dependencies.get_risk_manager` only inserts it
when ``settings.risk_use_ai_confidence`` is True. Default is False so
the FASE 8 risk chain stays untouched until the operator opts in.

Threshold default: 0.55 (slightly above the validator's
:data:`MIN_CONFIDENCE_FOR_APPROVE` floor of 0.5 — the gate is a
second tier of defence, not a duplicate of the floor).

When ``signal.confidence_ai`` is ``None`` (validator never ran or its
result wasn't backpopulated yet), the gate PASSES the signal. This
is deliberate so a transient validator outage doesn't paralyse the
trading loop; the validator is the canonical gate, this one is
belt-and-suspenders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mib.trading.risk.protocol import GateResult

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


DEFAULT_MIN_AI_CONFIDENCE: float = 0.55


class MinAIConfidenceGate:
    """Reject signals whose ``confidence_ai`` is below threshold."""

    name: ClassVar[str] = "min_ai_confidence"

    def __init__(
        self, threshold: float = DEFAULT_MIN_AI_CONFIDENCE
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(
                f"threshold must be in [0, 1] (got {threshold})"
            )
        self._threshold = threshold

    async def check(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,  # noqa: ARG002
        settings: Settings,  # noqa: ARG002
    ) -> GateResult:
        confidence = signal.confidence_ai
        if confidence is None:
            # Validator hasn't run / hasn't been backpopulated. Don't
            # block on missing data — the validator is the canonical
            # check. This gate is a second-tier defence.
            return GateResult(
                passed=True,
                reason="no confidence_ai (validator skipped or pending)",
                gate_name=self.name,
            )
        if confidence < self._threshold:
            return GateResult(
                passed=False,
                reason=(
                    f"confidence_ai={confidence:.2f} < threshold "
                    f"{self._threshold:.2f}"
                ),
                gate_name=self.name,
            )
        return GateResult(
            passed=True,
            reason=(
                f"confidence_ai={confidence:.2f} >= threshold "
                f"{self._threshold:.2f}"
            ),
            gate_name=self.name,
        )
