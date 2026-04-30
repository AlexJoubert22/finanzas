"""Temporal guards for mode transitions (FASE 10.3).

This module is imported lazily by :class:`ModeService` so 10.1 can
ship a working ``/mode`` command before the real guards land in 10.3.
The 10.1 stub permits all transitions (the test suite at 10.1 only
exercises ``OFF -> SHADOW`` which is unconditionally allowed in the
final ruleset anyway). 10.3 replaces ``check_transition_allowed``
with the hardcoded-rules version.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.trading.mode import TradingMode


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str | None = None


async def check_transition_allowed(  # noqa: ARG001 — session unused in stub
    *,
    from_mode: TradingMode,
    to_mode: TradingMode,
    session_factory: async_sessionmaker[AsyncSession],
    reason: str | None = None,
) -> GuardResult:
    """FASE 10.1 stub: permit everything. Real rules land in 10.3."""
    _ = (from_mode, to_mode, session_factory, reason)
    return GuardResult(allowed=True)
