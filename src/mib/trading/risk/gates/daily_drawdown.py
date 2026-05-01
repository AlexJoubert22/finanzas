"""DailyDrawdownGate — gate #2 in the priority chain.

Computes today's realised PnL by summing the ``realized_pnl_quote``
column of trades closed since UTC midnight. If this falls below
``-daily_dd_max_pct × equity_quote``, the gate flips
``trading_state.killed_until`` to the next UTC midnight via the state
service and rejects. Subsequent signals will be rejected by
:class:`KillSwitchGate` (cheaper) until the kill window expires.

FASE 8.3 robustness: the ``trades`` table doesn't exist until FASE 9.
The PnL query catches :class:`OperationalError` and returns ``0`` so
this gate is wirable today without breaking the chain. Once trades
arrive, the same code path lights up automatically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.logger import logger
from mib.trading.risk.protocol import GateResult
from mib.trading.risk.state import TradingStateService

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.observability.emitter import IncidentEmitter as IncidentEmitterProto
    from mib.trading.signals import Signal


class DailyDrawdownGate:
    """Track today's realised PnL; flip kill window on breach."""

    name: ClassVar[str] = "daily_drawdown"

    def __init__(
        self,
        state_service: TradingStateService,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: type[datetime] = datetime,
        incident_emitter: IncidentEmitterProto | None = None,
    ) -> None:
        self._state = state_service
        self._sf = session_factory
        self._clock = clock
        self._incidents = incident_emitter

    async def check(
        self,
        signal: Signal,  # noqa: ARG002 — signature defined by Gate protocol
        portfolio: PortfolioSnapshot,
        settings: Settings,  # noqa: ARG002
    ) -> GateResult:
        state = await self._state.get()
        now = self._clock.now(UTC)

        # If a kill window is already active, defer to KillSwitchGate's
        # message — but report cleanly here in case our gate runs solo.
        if state.killed_until is not None:
            killed_until = state.killed_until
            if killed_until.tzinfo is None:
                killed_until = killed_until.replace(tzinfo=UTC)
            if now < killed_until:
                return GateResult(
                    passed=False,
                    reason=(
                        f"daily DD kill window already active until "
                        f"{killed_until.isoformat()}"
                    ),
                    gate_name=self.name,
                )

        starting_equity = portfolio.equity_quote
        if starting_equity == 0:
            return GateResult(
                passed=True,
                reason="no equity to compute daily DD against",
                gate_name=self.name,
            )

        today_pnl = await self._compute_today_pnl(now=now)
        threshold = -Decimal(str(state.daily_dd_max_pct)) * starting_equity

        if today_pnl < threshold:
            tomorrow_midnight = (
                now.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1)
            )
            await self._state.update(
                actor=f"gate:{self.name}",
                killed_until=tomorrow_midnight.replace(tzinfo=None),
            )
            # FASE 13.3: emit KILL_SWITCH_DD_DAILY incident.
            if self._incidents is not None:
                try:
                    from mib.observability.incidents import (  # noqa: PLC0415
                        CriticalIncidentType,
                    )

                    await self._incidents.emit(
                        type_=CriticalIncidentType.KILL_SWITCH_DD_DAILY,
                        context={
                            "today_pnl": str(today_pnl),
                            "threshold": str(threshold),
                            "starting_equity": str(starting_equity),
                            "killed_until": tomorrow_midnight.isoformat(),
                        },
                        severity="critical",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "daily_drawdown: incident emit failed: {}", exc
                    )
            return GateResult(
                passed=False,
                reason=(
                    f"daily DD breached: today_pnl={today_pnl} "
                    f"< threshold={threshold}; killed until "
                    f"{tomorrow_midnight.isoformat()}"
                ),
                gate_name=self.name,
            )

        return GateResult(
            passed=True,
            reason=f"daily PnL {today_pnl} within threshold {threshold}",
            gate_name=self.name,
        )

    async def _compute_today_pnl(self, *, now: datetime) -> Decimal:
        """Sum realised PnL from closed trades since UTC midnight today.

        Returns ``Decimal(0)`` and logs a warning when the ``trades``
        table does not exist yet (FASE 9 introduces it). This keeps
        the gate functional in FASE 8.3 while still ready for the day
        the table arrives.
        """
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # SQLite stores naive UTC; strip tzinfo for the comparison.
        midnight_naive = midnight.replace(tzinfo=None)

        try:
            async with self._sf() as session:
                stmt = text(
                    "SELECT COALESCE(SUM(realized_pnl_quote), 0) "
                    "FROM trades "
                    "WHERE closed_at >= :midnight"
                )
                result = await session.execute(
                    stmt, {"midnight": midnight_naive}
                )
                value = result.scalar()
        except OperationalError as exc:
            logger.warning(
                "daily_drawdown: trades table not available yet (FASE 9): {}",
                exc,
            )
            return Decimal(0)

        return Decimal(str(value)) if value is not None else Decimal(0)
