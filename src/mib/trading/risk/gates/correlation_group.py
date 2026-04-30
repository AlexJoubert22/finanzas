"""CorrelationGroupGate — caps combined exposure across correlated assets.

Loads ``config/correlation_groups.yaml`` once at construction. For an
incoming signal:

1. Find every group containing ``signal.ticker``.
2. For each group, sum the realized + sized-pending exposure of every
   member (same logic as :class:`ExposurePerTickerGate` but aggregated
   over the group).
3. If any group's combined exposure ≥ ``group_max_pct × equity``,
   reject. Strictest cap effectively wins because the first failing
   group rejects.

Tickers not in any group pass without comment — they're not
correlation-bound by the operator's taxonomy.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from mib.logger import logger
from mib.trading.risk.correlation_groups import (
    CorrelationGroup,
    CorrelationGroups,
)
from mib.trading.risk.protocol import GateResult
from mib.trading.risk.repo import RiskDecisionRepository
from mib.trading.signal_repo import SignalRepository

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


class CorrelationGroupGate:
    """Reject when a correlation group's combined exposure breaches its cap."""

    name: ClassVar[str] = "correlation_group"

    def __init__(
        self,
        groups: CorrelationGroups,
        signal_repo: SignalRepository,
        decision_repo: RiskDecisionRepository,
    ) -> None:
        self._groups = groups
        self._signals = signal_repo
        self._decisions = decision_repo

    async def check(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        settings: Settings,  # noqa: ARG002
    ) -> GateResult:
        equity = portfolio.equity_quote
        if equity == 0:
            return GateResult(
                passed=True,
                reason="no equity to compute correlation cap",
                gate_name=self.name,
            )

        ticker = signal.ticker
        relevant = self._groups.groups_for_ticker(ticker)
        if not relevant:
            return GateResult(
                passed=True,
                reason=f"{ticker} is not in any correlation group",
                gate_name=self.name,
            )

        for group in relevant:
            combined = await self._combined_exposure(group, portfolio)
            cap = Decimal(str(group.max_pct)) * equity
            if combined >= cap:
                return GateResult(
                    passed=False,
                    reason=(
                        f"group {group.name!r} combined exposure {combined} "
                        f">= cap {cap} ({group.max_pct:.0%} of {equity})"
                    ),
                    gate_name=self.name,
                )
        return GateResult(
            passed=True,
            reason=(
                f"{ticker} in {len(relevant)} group(s); all under their caps"
            ),
            gate_name=self.name,
        )

    async def _combined_exposure(
        self, group: CorrelationGroup, portfolio: PortfolioSnapshot
    ) -> Decimal:
        total = Decimal(0)
        # Realized — sum across every member symbol.
        for p in portfolio.positions:
            if p.symbol in group.members:
                total += abs(p.amount) * p.mark_price
        # Sized pending — for each member, sum sized_amount of the
        # latest decision for any consumed signal of that member.
        for member in group.members:
            consumed = await self._signals.list_by_ticker_and_status(
                member, "consumed"
            )
            for ps in consumed:
                decision = await self._decisions.latest_for_signal(ps.id)
                if decision is None or decision.sized_amount is None:
                    continue
                total += decision.sized_amount
        logger.debug(
            "correlation_group: group={} combined={}", group.name, total
        )
        return total
