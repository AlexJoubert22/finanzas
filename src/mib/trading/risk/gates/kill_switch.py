"""KillSwitchGate — gate #1 in the priority chain.

Reads ``trading_state.enabled`` and ``trading_state.killed_until``
from the singleton row via :class:`TradingStateService`. The cheapest
possible reject path: a single indexed PK lookup. If the bot has been
manually stopped (``/stop`` Telegram command in FASE 8.7) or the
daily-DD gate has flipped the kill window, every signal short-circuits
here without further evaluation.

This gate NEVER mutates ``trading_state``; it only reads. The DD gate
(:mod:`daily_drawdown`) is the only one that writes the kill window.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from mib.trading.risk.protocol import GateResult
from mib.trading.risk.state import TradingStateService

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


class KillSwitchGate:
    """Reject when ``trading_state.enabled`` is False or kill window is active."""

    name: ClassVar[str] = "kill_switch"

    def __init__(
        self,
        state_service: TradingStateService,
        *,
        clock: type[datetime] = datetime,
    ) -> None:
        self._state = state_service
        self._clock = clock

    async def check(
        self,
        signal: Signal,  # noqa: ARG002 — signature defined by Gate protocol
        portfolio: PortfolioSnapshot,  # noqa: ARG002
        settings: Settings,  # noqa: ARG002
    ) -> GateResult:
        state = await self._state.get()

        if not state.enabled:
            return GateResult(
                passed=False,
                reason="trading_state.enabled is False",
                gate_name=self.name,
            )

        if state.killed_until is not None:
            now = self._clock.now(UTC)
            killed_until = state.killed_until
            if killed_until.tzinfo is None:
                killed_until = killed_until.replace(tzinfo=UTC)
            if now < killed_until:
                return GateResult(
                    passed=False,
                    reason=(
                        f"kill window active until {killed_until.isoformat()}"
                    ),
                    gate_name=self.name,
                )

        return GateResult(
            passed=True,
            reason="kill switch open",
            gate_name=self.name,
        )
