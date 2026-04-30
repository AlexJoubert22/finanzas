"""Trading mode service (FASE 10.1).

Reads / writes the ``trading_state.mode`` column and persists every
transition into ``mode_transitions`` (append-only, FASE 10.2). The
service is the only allowed writer of the mode field — direct
``UPDATE trading_state SET mode=...`` from anywhere else is forbidden
by convention so the audit trail in ``mode_transitions`` stays
canonical.

Guards land in FASE 10.3 as a separate module so this service stays
small. ``transition_to`` accepts a ``force=True`` kwarg that bypasses
the guards; FASE 10.5 wires that through ``/mode_force`` with extra
audit constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.logger import logger
from mib.trading.mode import TradingMode
from mib.trading.risk.state import TradingStateService

if TYPE_CHECKING:  # pragma: no cover
    from mib.trading.mode_transitions_repo import ModeTransitionRepository


@dataclass(frozen=True)
class ModeTransitionResult:
    """Outcome of one ``transition_to`` call.

    ``allowed=False`` results never write to the DB; the caller can
    surface ``reason`` directly to the operator.
    """

    allowed: bool
    from_mode: TradingMode
    to_mode: TradingMode
    reason: str | None = None
    transition_id: int | None = None
    """PK of the persisted ``mode_transitions`` row when allowed."""


class ModeService:
    """Reads + transitions the persisted trading mode.

    Construct with the global ``async_session_factory`` and the
    :class:`TradingStateService` so we share the same engine and
    ordering semantics.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        state_service: TradingStateService,
        transitions_repo: ModeTransitionRepository | None = None,
    ) -> None:
        self._sf = session_factory
        self._state = state_service
        self._transitions = transitions_repo

    async def get_current(self) -> TradingMode:
        """Read the current mode from ``trading_state``.

        Falls back to ``OFF`` if the column carries an unknown value
        — defensive, in case a hand-edited DB makes it past startup.
        """
        snap = await self._state.get()
        try:
            return TradingMode(snap.mode)
        except ValueError:
            logger.warning(
                "mode_service: unknown mode value {!r} in DB; coercing to OFF",
                snap.mode,
            )
            return TradingMode.OFF

    async def transition_to(
        self,
        target: TradingMode,
        *,
        actor: str,
        reason: str | None = None,
        force: bool = False,
    ) -> ModeTransitionResult:
        """Attempt a transition. Validates guards (FASE 10.3) unless
        ``force=True``. On success: updates ``trading_state.mode`` and
        appends a ``mode_transitions`` row in the same logical step.
        """
        if not actor:
            raise ValueError("actor must be a non-empty audit string")

        current = await self.get_current()
        if current == target:
            return ModeTransitionResult(
                allowed=False,
                from_mode=current,
                to_mode=target,
                reason="no_op_transition",
            )

        # Guard check — imported here to avoid the circular wiring with
        # ``mode_transitions_repo`` during module load.
        if not force:
            from mib.trading.mode_guards import check_transition_allowed  # noqa: PLC0415

            verdict = await check_transition_allowed(
                from_mode=current,
                to_mode=target,
                session_factory=self._sf,
                reason=reason,
            )
            if not verdict.allowed:
                logger.info(
                    "mode_service: transition rejected {} -> {}: {}",
                    current,
                    target,
                    verdict.reason,
                )
                return ModeTransitionResult(
                    allowed=False,
                    from_mode=current,
                    to_mode=target,
                    reason=verdict.reason,
                )

        # Persist: update cache + append audit row in one transaction.
        now = datetime.now(UTC).replace(tzinfo=None)
        await self._state.update(actor=actor, mode=target.value)
        transition_id: int | None = None
        if self._transitions is not None:
            transition_id = await self._transitions.add(
                from_mode=current,
                to_mode=target,
                actor=actor,
                reason=reason,
                transitioned_at=now,
                override_used=force,
                mode_started_at_after_transition=now,
            )
        logger.info(
            "mode_service: {} -> {} actor={} force={} reason={!r}",
            current,
            target,
            actor,
            force,
            reason,
        )
        return ModeTransitionResult(
            allowed=True,
            from_mode=current,
            to_mode=target,
            reason=reason,
            transition_id=transition_id,
        )
