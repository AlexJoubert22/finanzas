"""Risk manager: runs gates in priority order, returns a :class:`RiskDecision`.

The manager is **pure**: it produces an immutable decision and never
persists. Persistence is the repository's job
(:class:`mib.trading.risk.repo.RiskDecisionRepository`). This split
mirrors :class:`mib.trading.strategy.StrategyEngine` from FASE 7.4 —
the same backtester (FASE 12) reuses the manager without polluting
production tables.

Gate ordering matters: cheapest rejects first. FASE 8.3 registers
:class:`KillSwitchGate` (DB-cached read) ahead of
:class:`DailyDrawdownGate` (joins ``trades`` for today's PnL). FASE
8.4a-d add the per-ticker / correlation / concurrency / rate-limit
gates further down the chain.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mib.config import get_settings
from mib.logger import logger
from mib.models.portfolio import PortfolioSnapshot
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.protocol import Gate, GateResult
from mib.trading.signals import PersistedSignal


class RiskManager:
    """Iterate over gates, short-circuit on first reject."""

    def __init__(self, gates: list[Gate]) -> None:
        self._gates: list[Gate] = list(gates)

    @property
    def gates(self) -> tuple[Gate, ...]:
        """Read-only view of registered gates in evaluation order."""
        return tuple(self._gates)

    async def evaluate(
        self,
        persisted: PersistedSignal,
        portfolio: PortfolioSnapshot,
        *,
        version: int = 1,
    ) -> RiskDecision:
        """Run gates over ``persisted.signal`` and return a decision.

        ``version`` is the explicit version that the caller will assign
        when persisting. Default is 1 — sufficient for first
        evaluation. Subsequent evaluations of the same signal must
        pass an incremented version (computed via
        :meth:`RiskDecisionRepository.next_version_for`); the
        :meth:`append_with_retry` helper handles this automatically.
        """
        settings = get_settings()
        signal = persisted.signal
        results: list[GateResult] = []
        approved = True

        for gate in self._gates:
            result = await gate.check(signal, portfolio, settings)
            results.append(result)
            if not result.passed:
                approved = False
                logger.info(
                    "risk_manager: rejected signal_id={} by gate={} reason={}",
                    persisted.id,
                    gate.name,
                    result.reason,
                )
                break  # short-circuit on first reject

        rejecting_gate = results[-1].gate_name if (results and not approved) else None
        if approved:
            reasoning = (
                f"approved after {len(results)} gate(s): "
                + ", ".join(r.gate_name for r in results)
            )
        else:
            reasoning = f"rejected by {rejecting_gate}: {results[-1].reason}"

        return RiskDecision(
            signal_id=persisted.id,
            version=version,
            approved=approved,
            gate_results=tuple(results),
            reasoning=reasoning,
            decided_at=datetime.now(UTC),
            sized_amount=None,  # filled in FASE 8.5
        )
