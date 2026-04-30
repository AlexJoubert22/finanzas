"""Temporal guards for mode transitions (FASE 10.3).

The rules are hardcoded — any change requires an explicit code PR
because they encode the project's discipline ladder. Hardcoding (vs
config-driven) is intentional: a config flip is too cheap a way to
shortcut the ladder.

Rules:
- ``OFF -> SHADOW``     : free.
- ``SHADOW -> PAPER``   : ≥14 days in SHADOW continuously.
- ``PAPER -> SEMI_AUTO``: ≥30 days in PAPER + ≥50 trades closed in PAPER.
- ``SEMI_AUTO -> LIVE`` : ≥60 days in SEMI_AUTO + ``days_clean_streak() >= 60``
                          (placeholder returning 0 until FASE 13 wires
                          the real metric — so LIVE is unreachable
                          without ``/mode_force`` until then).
- ``* -> OFF``          : free (defensive regression always permitted).
- ``LIVE -> PAPER``, etc. (regressions): free BUT ``reason`` is mandatory.
- Same -> same          : ``no_op_transition`` rejection.

The "days in mode" measurement reads ``mode_transitions.latest_into
(current_mode)`` — i.e. the mode_started_at_after_transition of the
last transition that landed on the current mode. If no transition
exists yet (cold-start, mode lives only in the seeded
trading_state.mode column), the guard counts elapsed time as 0 days
and rejects forward steps. Operators bring the bot up via
``OFF -> SHADOW`` first, which seeds the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import ModeTransitionRow, TradeRow
from mib.trading.mode import TradingMode

# ─── Hardcoded thresholds ────────────────────────────────────────────

#: Minimum days an upward transition needs in the source mode.
MIN_DAYS_PER_TRANSITION: dict[
    tuple[TradingMode, TradingMode], int
] = {
    (TradingMode.SHADOW, TradingMode.PAPER): 14,
    (TradingMode.PAPER, TradingMode.SEMI_AUTO): 30,
    (TradingMode.SEMI_AUTO, TradingMode.LIVE): 60,
}

#: Per-transition trade-count thresholds. Only PAPER -> SEMI_AUTO
#: requires this in 10.3; LIVE adds the days_clean_streak gate.
MIN_CLOSED_TRADES_PER_TRANSITION: dict[
    tuple[TradingMode, TradingMode], int
] = {
    (TradingMode.PAPER, TradingMode.SEMI_AUTO): 50,
}

#: Days of clean streak required for SEMI_AUTO → LIVE. Wired to the
#: real :func:`mib.observability.clean_streak.days_clean_streak` in
#: FASE 13; until then the placeholder returns 0 which keeps LIVE
#: behind ``/mode_force`` only.
MIN_DAYS_CLEAN_STREAK_FOR_LIVE: int = 60


@dataclass(frozen=True)
class GuardResult:
    """Outcome of one ``check_transition_allowed`` call."""

    allowed: bool
    reason: str | None = None


# ─── Public API ──────────────────────────────────────────────────────


async def check_transition_allowed(
    *,
    from_mode: TradingMode,
    to_mode: TradingMode,
    session_factory: async_sessionmaker[AsyncSession],
    reason: str | None = None,
) -> GuardResult:
    """Evaluate the hardcoded ladder. Pure-DB read; no side effects."""
    # Same -> same is the cheapest reject; surface it explicitly even
    # though ModeService also short-circuits this path for safety.
    if from_mode == to_mode:
        return GuardResult(allowed=False, reason="no_op_transition")

    # Defensive regression to OFF is always allowed.
    if to_mode == TradingMode.OFF:
        return GuardResult(allowed=True)

    if _is_regression(from_mode, to_mode):
        if not reason or not reason.strip():
            return GuardResult(
                allowed=False,
                reason="regression_requires_reason",
            )
        return GuardResult(allowed=True)

    # OFF -> SHADOW is the canonical first step; no temporal gate.
    if from_mode == TradingMode.OFF and to_mode == TradingMode.SHADOW:
        return GuardResult(allowed=True)

    # Forward (upward) transitions: validate days-in-mode + extras.
    days_required = MIN_DAYS_PER_TRANSITION.get((from_mode, to_mode))
    if days_required is None:
        # Any forward path not in the ladder map (e.g. SHADOW -> LIVE)
        # is rejected as 'unknown_path' — the operator must climb the
        # ladder one step at a time.
        return GuardResult(
            allowed=False,
            reason=f"unknown_ladder_path:{from_mode.value}->{to_mode.value}",
        )

    days = await days_in_current_mode(from_mode, session_factory)
    if days < days_required:
        return GuardResult(
            allowed=False,
            reason=(
                f"insufficient_time_in_mode:"
                f"{days}d_in_{from_mode.value}_need_{days_required}d"
            ),
        )

    trades_required = MIN_CLOSED_TRADES_PER_TRANSITION.get(
        (from_mode, to_mode)
    )
    if trades_required is not None:
        closed = await closed_trades_in_mode(
            from_mode, session_factory
        )
        if closed < trades_required:
            return GuardResult(
                allowed=False,
                reason=(
                    f"insufficient_closed_trades:"
                    f"{closed}_in_{from_mode.value}_need_{trades_required}"
                ),
            )

    if to_mode == TradingMode.LIVE:
        streak = days_clean_streak()
        if streak < MIN_DAYS_CLEAN_STREAK_FOR_LIVE:
            return GuardResult(
                allowed=False,
                reason=(
                    f"insufficient_clean_streak:"
                    f"{streak}d_need_{MIN_DAYS_CLEAN_STREAK_FOR_LIVE}d"
                ),
            )

    return GuardResult(allowed=True)


# ─── Helpers (public so 10.4 /mode_status can re-use them) ──────────


async def days_in_current_mode(
    mode: TradingMode,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Days elapsed since the most recent transition INTO ``mode``.

    Returns 0 if no audit row matches (cold-start or mode never been
    entered). Caller decides whether 0 is enough; this helper does
    not bake in the threshold.
    """
    async with session_factory() as session:
        stmt = (
            select(ModeTransitionRow.mode_started_at_after_transition)
            .where(ModeTransitionRow.to_mode == mode.value)
            .order_by(ModeTransitionRow.transitioned_at.desc())
            .limit(1)
        )
        anchor = (await session.scalars(stmt)).first()
    if anchor is None:
        return 0
    delta = datetime.now(UTC).replace(tzinfo=None) - anchor
    return max(int(delta.total_seconds() // 86400), 0)


async def closed_trades_in_mode(
    mode: TradingMode,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Count of ``trades`` with ``closed_at`` while ``mode`` was active.

    Implementation: query ``trades.closed_at`` and check it falls
    inside any ``[transition_into_mode, transition_out_of_mode)``
    window from ``mode_transitions``. The naive "all closed_at >
    last_transition_into_mode" can over-count if the operator
    bounced between modes.
    """
    async with session_factory() as session:
        # Build the (start, end) windows the mode was active.
        stmt = (
            select(
                ModeTransitionRow.to_mode,
                ModeTransitionRow.mode_started_at_after_transition,
                ModeTransitionRow.transitioned_at,
            )
            .order_by(ModeTransitionRow.transitioned_at.asc())
        )
        rows = (await session.execute(stmt)).all()

        windows: list[tuple[datetime, datetime]] = []
        current_start: datetime | None = None
        for to_mode, started_at, _ in rows:
            if to_mode == mode.value:
                current_start = started_at
            elif current_start is not None:
                # Mode was exited at this transitioned_at.
                windows.append((current_start, _))
                current_start = None
        if current_start is not None:
            windows.append(
                (current_start, datetime.now(UTC).replace(tzinfo=None))
            )

        if not windows:
            return 0

        # Count trades closed inside any window.
        total = 0
        for start, end in windows:
            count_stmt = select(func.count(TradeRow.id)).where(
                TradeRow.status == "closed",
                TradeRow.closed_at.is_not(None),
                TradeRow.closed_at >= start,
                TradeRow.closed_at < end,
            )
            count = (await session.execute(count_stmt)).scalar_one()
            total += int(count or 0)
    return total


def days_clean_streak() -> int:
    """Placeholder: returns 0 until FASE 13 wires the real metric.

    TODO FASE 13: replace with a query against ``critical_incidents``
    that returns days since the last reset trigger (>24h to resolve
    or a severe-type incident). Until then, LIVE remains unreachable
    without ``/mode_force``.
    """
    return 0


# ─── Internal ────────────────────────────────────────────────────────


_MODE_RANK: dict[TradingMode, int] = {
    TradingMode.OFF: 0,
    TradingMode.SHADOW: 1,
    TradingMode.PAPER: 2,
    TradingMode.SEMI_AUTO: 3,
    TradingMode.LIVE: 4,
}


def _is_regression(from_mode: TradingMode, to_mode: TradingMode) -> bool:
    """True iff ``to_mode`` is below ``from_mode`` in the ladder.

    OFF is the lowest; LIVE the highest. Equal rank is not a
    regression (caller already filters same -> same earlier).
    """
    return _MODE_RANK[to_mode] < _MODE_RANK[from_mode]


# Re-exported for tests that want to construct windows manually.
__all__ = [
    "MIN_DAYS_PER_TRANSITION",
    "MIN_CLOSED_TRADES_PER_TRANSITION",
    "MIN_DAYS_CLEAN_STREAK_FOR_LIVE",
    "GuardResult",
    "check_transition_allowed",
    "days_in_current_mode",
    "closed_trades_in_mode",
    "days_clean_streak",
]


# Suppress unused-import warning for timedelta (kept for potential
# future fine-grained windows in 10.4).
_ = timedelta
