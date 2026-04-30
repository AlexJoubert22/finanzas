"""MaxConcurrentTradesGate — hard cap on simultaneously open positions.

Counts open positions in :class:`PortfolioSnapshot.positions` (i.e.
positions with non-zero amount). Rejects new signals when at or over
``max_concurrent_trades``. Defensive against runaway signal storms
that would over-leverage the system.

Note: spot exchanges report no positions — only balances. For spot
we count open *signals* in status='consumed' as proxy positions until
FASE 9 lands the trades table. This cleanly handles both worlds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mib.trading.risk.protocol import GateResult
from mib.trading.signal_repo import SignalRepository

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


class MaxConcurrentTradesGate:
    """Reject when N or more positions/consumed-signals are already open."""

    name: ClassVar[str] = "max_concurrent_trades"

    def __init__(self, signal_repo: SignalRepository) -> None:
        self._signals = signal_repo

    async def check(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio: PortfolioSnapshot,
        settings: Settings,
    ) -> GateResult:
        cap = settings.max_concurrent_trades
        # Realized: futures positions reported by the exchange.
        realized = len(portfolio.positions)
        # Consumed-but-unexecuted signals act as proxy positions in
        # FASE 8 (trades table only arrives in FASE 9).
        # Sum across known consumed signals from the repo.
        pending = await self._count_consumed_signals()
        total = realized + pending

        if total >= cap:
            return GateResult(
                passed=False,
                reason=(
                    f"open positions/pending = {total} >= cap {cap} "
                    f"(realized={realized}, consumed_pending={pending})"
                ),
                gate_name=self.name,
            )
        return GateResult(
            passed=True,
            reason=f"open positions/pending = {total} < cap {cap}",
            gate_name=self.name,
        )

    async def _count_consumed_signals(self) -> int:
        # Best-effort proxy. FASE 9 will replace this with a direct
        # count of open trades.
        from mib.trading.signals import SIGNAL_STATUSES  # noqa: PLC0415

        # We don't have a direct count helper, so list_pending et al
        # don't apply. Reuse list_by_ticker_and_status across the
        # known-tickers set is wrong. Simplest: query consumed status
        # via a custom helper we add inline here using the repo's
        # session_factory. To avoid leaking impl into the gate, we
        # delegate to a small repo helper. For FASE 8.4c, use a small
        # internal helper that queries directly.
        assert "consumed" in SIGNAL_STATUSES
        return await _count_signals_by_status(self._signals, "consumed")


async def _count_signals_by_status(
    repo: SignalRepository, status: str, *, limit: int = 1000
) -> int:
    """List + len shortcut. List is bounded by ``limit`` so a runaway
    signal storm doesn't OOM the gate; in practice ``max_concurrent``
    is single-digit so this is fine.
    """
    # The repo has no count() but has list_by_ticker_and_status which
    # filters by ticker. For a "all consumed regardless of ticker"
    # count we use a small ad-hoc query via the session_factory.
    from sqlalchemy import func, select  # noqa: PLC0415

    from mib.db.models import SignalRow  # noqa: PLC0415

    sf = repo._sf  # noqa: SLF001 — internal access for count optimisation
    async with sf() as session:
        stmt = select(func.count(SignalRow.id)).where(SignalRow.status == status)
        n = (await session.scalar(stmt)) or 0
        return min(int(n), limit)
