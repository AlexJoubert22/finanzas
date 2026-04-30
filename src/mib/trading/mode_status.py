"""Projection helper for ``/mode_status`` (FASE 10.4).

Builds a structured snapshot of:
- the current mode + how long we've been in it,
- the most recent transition (if any),
- the next allowed mode plus the gates that still need to clear.

Pure read; no side effects. Returns a dataclass the Telegram handler
formats; tests don't need a Telegram client.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.trading.mode import TradingMode
from mib.trading.mode_guards import (
    MIN_CLOSED_TRADES_PER_TRANSITION,
    MIN_DAYS_CLEAN_STREAK_FOR_LIVE,
    MIN_DAYS_PER_TRANSITION,
    closed_trades_in_mode,
    days_clean_streak,
    days_in_current_mode,
)
from mib.trading.mode_transitions_repo import (
    ModeTransition,
    ModeTransitionRepository,
)

#: Forward path each mode can climb to next. LIVE has none.
_NEXT_FORWARD: dict[TradingMode, TradingMode | None] = {
    TradingMode.OFF: TradingMode.SHADOW,
    TradingMode.SHADOW: TradingMode.PAPER,
    TradingMode.PAPER: TradingMode.SEMI_AUTO,
    TradingMode.SEMI_AUTO: TradingMode.LIVE,
    TradingMode.LIVE: None,
}


@dataclass(frozen=True)
class ProgressGate:
    """One requirement the operator can see clearly: have / need."""

    name: str
    have: int
    need: int

    @property
    def met(self) -> bool:
        return self.have >= self.need

    @property
    def remaining(self) -> int:
        return max(self.need - self.have, 0)


@dataclass(frozen=True)
class ModeStatus:
    """Full picture of where we are + where we can go next."""

    current: TradingMode
    days_in_current: int
    last_transition: ModeTransition | None
    next_mode: TradingMode | None
    gates: tuple[ProgressGate, ...]
    """Empty when ``next_mode`` is None (LIVE) or no gates apply (OFF)."""


async def build_mode_status(
    *,
    current: TradingMode,
    transitions_repo: ModeTransitionRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> ModeStatus:
    """Compose the projection. Pure read; no DB mutations."""
    days_in_current = await days_in_current_mode(
        current, session_factory
    )
    last = await transitions_repo.latest()
    next_mode = _NEXT_FORWARD.get(current)
    gates: list[ProgressGate] = []

    if next_mode is not None and current != TradingMode.OFF:
        days_required = MIN_DAYS_PER_TRANSITION.get((current, next_mode))
        if days_required is not None:
            gates.append(
                ProgressGate(
                    name="days_in_mode",
                    have=days_in_current,
                    need=days_required,
                )
            )
        trades_required = MIN_CLOSED_TRADES_PER_TRANSITION.get(
            (current, next_mode)
        )
        if trades_required is not None:
            closed = await closed_trades_in_mode(
                current, session_factory
            )
            gates.append(
                ProgressGate(
                    name="closed_trades",
                    have=closed,
                    need=trades_required,
                )
            )
        if next_mode == TradingMode.LIVE:
            gates.append(
                ProgressGate(
                    name="days_clean_streak",
                    have=days_clean_streak(),
                    need=MIN_DAYS_CLEAN_STREAK_FOR_LIVE,
                )
            )

    return ModeStatus(
        current=current,
        days_in_current=days_in_current,
        last_transition=last,
        next_mode=next_mode,
        gates=tuple(gates),
    )


def format_mode_status_html(status: ModeStatus) -> str:
    """Render a Telegram-ready HTML message from a :class:`ModeStatus`."""
    from mib.telegram.formatters import esc  # noqa: PLC0415

    lines = [
        "📊 <b>Mode status</b>",
        f"  modo actual: <code>{esc(status.current.value)}</code>",
        f"  días en este modo: <code>{status.days_in_current}</code>",
    ]
    if status.last_transition is not None:
        lt = status.last_transition
        lines.extend([
            "",
            "<b>Última transición</b>",
            f"  <code>{esc(lt.from_mode.value)}</code> → "
            f"<code>{esc(lt.to_mode.value)}</code>",
            f"  cuándo: <code>{esc(str(lt.transitioned_at))}</code>",
            f"  actor: <code>{esc(lt.actor)}</code>",
            f"  reason: <code>{esc(lt.reason or '(none)')}</code>"
            + ("  <b>[FORCE]</b>" if lt.override_used else ""),
        ])

    if status.next_mode is None:
        lines.extend([
            "",
            "<i>Modo terminal — no hay siguiente forward.</i>",
        ])
    else:
        all_met = all(g.met for g in status.gates) if status.gates else True
        verdict_icon = "✅" if all_met else "⏳"
        lines.extend([
            "",
            f"{verdict_icon} <b>Próximo modo permitido:</b> "
            f"<code>{esc(status.next_mode.value)}</code>",
        ])
        if not status.gates:
            lines.append("  <i>(sin gates — transición libre)</i>")
        for g in status.gates:
            mark = "✅" if g.met else "⏳"
            line = (
                f"  {mark} {esc(g.name)}: "
                f"<code>{g.have}/{g.need}</code>"
            )
            if not g.met:
                line += f"  <i>(faltan {g.remaining})</i>"
            lines.append(line)

    return "\n".join(lines)
