"""Tests for ``mode_status`` projection (FASE 10.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.session import async_session_factory
from mib.trading.mode import TradingMode
from mib.trading.mode_status import (
    ProgressGate,
    build_mode_status,
    format_mode_status_html,
)
from mib.trading.mode_transitions_repo import ModeTransitionRepository


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed(
    *,
    from_mode: TradingMode,
    to_mode: TradingMode,
    days_ago: int,
    actor: str = "test",
    override: bool = False,
    reason: str | None = None,
) -> None:
    repo = ModeTransitionRepository(async_session_factory)
    when = _now() - timedelta(days=days_ago)
    await repo.add(
        from_mode=from_mode,
        to_mode=to_mode,
        actor=actor,
        reason=reason,
        transitioned_at=when,
        override_used=override,
        mode_started_at_after_transition=when,
    )


# ─── Pure ProgressGate ──────────────────────────────────────────────


def test_progress_gate_met_and_remaining() -> None:
    g = ProgressGate(name="x", have=10, need=14)
    assert g.met is False
    assert g.remaining == 4
    g2 = ProgressGate(name="x", have=20, need=14)
    assert g2.met is True
    assert g2.remaining == 0


# ─── build_mode_status ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_off_with_no_history(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.OFF,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.current == TradingMode.OFF
    assert status.last_transition is None
    assert status.next_mode == TradingMode.SHADOW
    # OFF → SHADOW has no gates (free transition); gates list is empty.
    assert status.gates == ()


@pytest.mark.asyncio
async def test_status_shadow_progress_unmet(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """In SHADOW for 5 days → days_in_mode gate unmet (5/14)."""
    await _seed(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW, days_ago=5
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.SHADOW,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.current == TradingMode.SHADOW
    assert status.days_in_current == 5
    assert status.next_mode == TradingMode.PAPER
    assert len(status.gates) == 1
    g = status.gates[0]
    assert g.name == "days_in_mode"
    assert g.have == 5
    assert g.need == 14
    assert g.remaining == 9
    assert g.met is False


@pytest.mark.asyncio
async def test_status_paper_with_two_gates(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """PAPER → SEMI_AUTO has two gates: days + closed_trades."""
    await _seed(
        from_mode=TradingMode.SHADOW, to_mode=TradingMode.PAPER, days_ago=20
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.PAPER,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.next_mode == TradingMode.SEMI_AUTO
    names = {g.name for g in status.gates}
    assert names == {"days_in_mode", "closed_trades"}
    days_gate = next(g for g in status.gates if g.name == "days_in_mode")
    trades_gate = next(g for g in status.gates if g.name == "closed_trades")
    assert days_gate.need == 30
    assert trades_gate.need == 50


@pytest.mark.asyncio
async def test_status_semi_auto_includes_clean_streak_gate(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """SEMI_AUTO → LIVE adds the days_clean_streak gate.

    FASE 13.5: streak now reads from critical_incidents. With an
    empty incidents table the streak hits MAX_REPORTABLE_STREAK_DAYS
    (365), so the gate is technically MET in cold-start tests; we
    add a recent severe incident to make the assertion meaningful.
    """
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime as _dt
    from datetime import timedelta as _td  # noqa: PLC0415

    from mib.observability.incidents import (  # noqa: PLC0415
        CriticalIncidentRepository,
        CriticalIncidentType,
    )

    await _seed(
        from_mode=TradingMode.PAPER, to_mode=TradingMode.SEMI_AUTO,
        days_ago=10,
    )
    incident_repo = CriticalIncidentRepository(async_session_factory)
    await incident_repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_dt.now(UTC).replace(tzinfo=None) - _td(days=5),
        auto_detected=True,
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.SEMI_AUTO,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.next_mode == TradingMode.LIVE
    names = {g.name for g in status.gates}
    assert "days_clean_streak" in names
    streak_gate = next(
        g for g in status.gates if g.name == "days_clean_streak"
    )
    # 5 days since severe incident -> below 60-day threshold.
    assert 4 <= streak_gate.have <= 5
    assert streak_gate.need == 60
    assert streak_gate.met is False


@pytest.mark.asyncio
async def test_status_live_is_terminal(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """LIVE has no next_mode; gates list is empty."""
    await _seed(
        from_mode=TradingMode.SEMI_AUTO, to_mode=TradingMode.LIVE,
        days_ago=1,
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.LIVE,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.next_mode is None
    assert status.gates == ()


@pytest.mark.asyncio
async def test_status_last_transition_populated(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW,
        days_ago=2, actor="user:42", reason="bring-up",
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.SHADOW,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    assert status.last_transition is not None
    assert status.last_transition.actor == "user:42"
    assert status.last_transition.reason == "bring-up"


# ─── HTML rendering ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_html_renders_progress(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW, days_ago=3
    )
    repo = ModeTransitionRepository(async_session_factory)
    status = await build_mode_status(
        current=TradingMode.SHADOW,
        transitions_repo=repo,
        session_factory=async_session_factory,
    )
    html = format_mode_status_html(status)
    assert "Mode status" in html
    assert "shadow" in html
    assert "Próximo modo permitido" in html
    assert "paper" in html
    assert "days_in_mode" in html
    assert "3/14" in html
    assert "faltan 11" in html


def test_format_html_terminal_live() -> None:
    from mib.trading.mode_status import ModeStatus  # noqa: PLC0415

    status = ModeStatus(
        current=TradingMode.LIVE,
        days_in_current=10,
        last_transition=None,
        next_mode=None,
        gates=(),
    )
    html = format_mode_status_html(status)
    assert "Modo terminal" in html
