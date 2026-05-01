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

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from mib.config import get_settings
from mib.logger import logger
from mib.models.portfolio import PortfolioSnapshot
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.protocol import Gate, GateResult
from mib.trading.signals import PersistedSignal
from mib.trading.sizing import PositionSizer

#: Window during which the first_30_days sizing modifier is active
#: after the most recent transition INTO LIVE.
LIVE_FIRST_30_DAYS_WINDOW: timedelta = timedelta(days=30)

LiveAnchorResolver = Callable[[], Awaitable[datetime | None]]


class RiskManager:
    """Iterate over gates, short-circuit on first reject."""

    def __init__(
        self,
        gates: list[Gate],
        *,
        sizer: PositionSizer | None = None,
        live_anchor_resolver: LiveAnchorResolver | None = None,
    ) -> None:
        self._gates: list[Gate] = list(gates)
        # Sizer is optional so gate-only tests can construct a manager
        # without piping the sizer dependency through. Production
        # always provides one (FASE 8.5 wiring in dependencies.py).
        self._sizer = sizer
        # FASE 14.3: optional async callable returning the timestamp of
        # the most recent transition INTO LIVE (None if never LIVE).
        # Used to gate the first-30-days sizing modifier.
        self._live_anchor_resolver = live_anchor_resolver

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

        # FASE 8.5: if the gates passed, run the sizer. A sizer that
        # returns 0 (e.g. min_notional unreachable) flips the decision
        # to approved=False with a composed reason.
        sized_amount = None
        if approved and self._sizer is not None:
            live_first_30d_active = await self._is_live_first_30d_active()
            sizer_result = self._sizer.size(
                signal,
                portfolio,
                settings,
                live_first_30d_active=live_first_30d_active,
            )
            if sizer_result.amount > 0:
                sized_amount = sizer_result.amount
                reasoning += f" | sized: {sizer_result.reasoning}"
            else:
                approved = False
                reasoning += f" | sizer rejected: {sizer_result.reasoning}"
                logger.info(
                    "risk_manager: sizer rejected signal_id={} reason={}",
                    persisted.id,
                    sizer_result.reasoning,
                )

        return RiskDecision(
            signal_id=persisted.id,
            version=version,
            approved=approved,
            gate_results=tuple(results),
            reasoning=reasoning,
            decided_at=datetime.now(UTC),
            sized_amount=sized_amount,
        )

    async def _is_live_first_30d_active(self) -> bool:
        """True iff a live_anchor_resolver is wired and the resolved
        anchor falls within :data:`LIVE_FIRST_30_DAYS_WINDOW`.

        Resolver failures swallow to ``False`` — the modifier is a
        safety reduction, not a gate, so a flaky lookup must never
        block a live signal. The error is logged for the operator.
        """
        if self._live_anchor_resolver is None:
            return False
        try:
            anchor = await self._live_anchor_resolver()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "risk_manager: live_anchor_resolver failed: {}", exc
            )
            return False
        if anchor is None:
            return False
        # Compare in naive UTC to match how transitions are persisted
        # (mode_transitions stores naive UTC via .replace(tzinfo=None)).
        anchor_naive = (
            anchor.astimezone(UTC).replace(tzinfo=None)
            if anchor.tzinfo is not None
            else anchor
        )
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        return (now_naive - anchor_naive) < LIVE_FIRST_30_DAYS_WINDOW
