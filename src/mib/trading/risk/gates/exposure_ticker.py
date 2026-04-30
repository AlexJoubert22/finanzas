"""ExposurePerTickerGate — cap exposure to a single ticker.

Counts:
1. Realized exposure: sum of ``|amount| × mark_price`` across open
   positions matching the ticker (PortfolioState).
2. Sized pending exposure: sum of ``RiskDecision.sized_amount`` for
   the latest decision of every signal in status ``consumed`` whose
   ticker matches. These are signals approved by the operator but not
   yet executed (FASE 9 will close that loop).

Reject when current total exposure ≥ ``max_exposure_per_ticker_pct ×
equity_quote``. The sizer in FASE 8.5 then saturates the new signal's
position to whatever headroom remains, so this gate only short-
circuits the obvious case "no headroom at all".

Until FASE 8.5 the sized-pending sum is structurally 0 (no decision
has ``sized_amount`` populated yet); the code path is in place from
day one to avoid a refactor when the sizer lands.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from mib.logger import logger
from mib.trading.risk.protocol import GateResult
from mib.trading.risk.repo import RiskDecisionRepository
from mib.trading.signal_repo import SignalRepository

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


class ExposurePerTickerGate:
    """Reject when exposure to ``signal.ticker`` already saturates the cap."""

    name: ClassVar[str] = "exposure_per_ticker"

    def __init__(
        self,
        signal_repo: SignalRepository,
        decision_repo: RiskDecisionRepository,
    ) -> None:
        self._signals = signal_repo
        self._decisions = decision_repo

    async def check(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        settings: Settings,
    ) -> GateResult:
        equity = portfolio.equity_quote
        if equity == 0:
            return GateResult(
                passed=True,
                reason="no equity to compute exposure cap",
                gate_name=self.name,
            )

        cap_pct = Decimal(str(settings.max_exposure_per_ticker_pct))
        cap_quote = cap_pct * equity

        ticker = signal.ticker
        realized = _realized_exposure(ticker, portfolio)
        pending = await self._sum_sized_pending(ticker)
        current = realized + pending

        if current >= cap_quote:
            return GateResult(
                passed=False,
                reason=(
                    f"exposure {current} on {ticker} >= cap {cap_quote} "
                    f"(realized={realized}, pending_sized={pending})"
                ),
                gate_name=self.name,
            )

        headroom = cap_quote - current
        return GateResult(
            passed=True,
            reason=(
                f"exposure {current} on {ticker} < cap {cap_quote}; "
                f"headroom {headroom}"
            ),
            gate_name=self.name,
        )

    async def _sum_sized_pending(self, ticker: str) -> Decimal:
        """Sum sized_amount of latest RiskDecision for each consumed signal of ticker.

        "consumed" is the signal's status after the operator approves
        but before the executor (FASE 9) opens the trade. While we
        don't have FASE 9 yet, this returns 0; the code path stays in
        place so the gate is correct on the day FASE 9 lands.
        """
        consumed = await self._signals.list_by_ticker_and_status(
            ticker, "consumed"
        )
        if not consumed:
            return Decimal(0)
        total = Decimal(0)
        for ps in consumed:
            decision = await self._decisions.latest_for_signal(ps.id)
            if decision is None or decision.sized_amount is None:
                continue
            total += decision.sized_amount
        logger.debug(
            "exposure_per_ticker: ticker={} sized_pending={} from {} signal(s)",
            ticker,
            total,
            len(consumed),
        )
        return total


def _realized_exposure(ticker: str, portfolio: PortfolioSnapshot) -> Decimal:
    total = Decimal(0)
    for p in portfolio.positions:
        if p.symbol == ticker:
            total += abs(p.amount) * p.mark_price
    return total
