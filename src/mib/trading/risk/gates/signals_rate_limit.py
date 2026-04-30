"""SignalsPerHourRateLimitGate — defensive rolling-window cap.

Counts ``signal_status_events`` rows of ``event_type='approved'``
within the last 60 minutes. Rejects when the count is at or over
``max_signals_per_hour``.

Rationale: even if every other gate passes, a healthy system
shouldn't be approving more than ~2 trades per hour outside HFT.
This catches runaway signal generation due to bugs or extreme
regimes — caps the blast radius before exposure compounds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import SignalStatusEvent
from mib.logger import logger
from mib.trading.risk.protocol import GateResult

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


class SignalsPerHourRateLimitGate:
    """Reject when ``approved`` events in the last 60 min ≥ cap."""

    name: ClassVar[str] = "signals_per_hour"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: type[datetime] = datetime,
    ) -> None:
        self._sf = session_factory
        self._clock = clock

    async def check(
        self,
        signal: Signal,  # noqa: ARG002
        portfolio: PortfolioSnapshot,  # noqa: ARG002
        settings: Settings,
    ) -> GateResult:
        cap = settings.max_signals_per_hour
        cutoff = (self._clock.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
        async with self._sf() as session:
            stmt = select(func.count(SignalStatusEvent.id)).where(
                SignalStatusEvent.event_type == "approved",
                SignalStatusEvent.created_at >= cutoff,
            )
            n = (await session.scalar(stmt)) or 0
            count = int(n)

        logger.debug(
            "signals_per_hour: approved in last 60min = {} (cap {})", count, cap
        )

        if count >= cap:
            return GateResult(
                passed=False,
                reason=(
                    f"approved signals in last 60min = {count} >= cap {cap}"
                ),
                gate_name=self.name,
            )
        return GateResult(
            passed=True,
            reason=f"approved in last 60min = {count} < cap {cap}",
            gate_name=self.name,
        )
