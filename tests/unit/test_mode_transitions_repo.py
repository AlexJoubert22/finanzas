"""Tests for :class:`ModeTransitionRepository` (FASE 10.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.models import ModeTransitionRow
from mib.db.session import async_session_factory
from mib.trading.mode import TradingMode
from mib.trading.mode_transitions_repo import ModeTransitionRepository


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def repo() -> ModeTransitionRepository:
    return ModeTransitionRepository(async_session_factory)


@pytest.mark.asyncio
async def test_add_returns_pk_and_persists(
    repo: ModeTransitionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    now = _now()
    new_id = await repo.add(
        from_mode=TradingMode.OFF,
        to_mode=TradingMode.SHADOW,
        actor="user:42",
        reason="bring-up",
        transitioned_at=now,
        override_used=False,
        mode_started_at_after_transition=now,
    )
    assert new_id > 0
    async with async_session_factory() as session:
        row = await session.get(ModeTransitionRow, new_id)
        assert row is not None
        assert row.from_mode == "off"
        assert row.to_mode == "shadow"
        assert row.actor == "user:42"
        assert row.reason == "bring-up"
        assert row.override_used is False


@pytest.mark.asyncio
async def test_latest_returns_most_recent(
    repo: ModeTransitionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = _now() - timedelta(hours=1)
    await repo.add(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW,
        actor="u", reason=None,
        transitioned_at=base, override_used=False,
        mode_started_at_after_transition=base,
    )
    later = base + timedelta(minutes=30)
    await repo.add(
        from_mode=TradingMode.SHADOW, to_mode=TradingMode.PAPER,
        actor="u", reason="t",
        transitioned_at=later, override_used=False,
        mode_started_at_after_transition=later,
    )
    latest = await repo.latest()
    assert latest is not None
    assert latest.from_mode == TradingMode.SHADOW
    assert latest.to_mode == TradingMode.PAPER


@pytest.mark.asyncio
async def test_latest_into_filters_by_to_mode(
    repo: ModeTransitionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = _now() - timedelta(days=2)
    await repo.add(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW,
        actor="u", reason=None,
        transitioned_at=base, override_used=False,
        mode_started_at_after_transition=base,
    )
    # Re-enter SHADOW later — guard's "days in current mode" anchors on
    # the most recent ``to_mode=shadow`` row, not the historical one.
    re_entry = base + timedelta(days=1)
    await repo.add(
        from_mode=TradingMode.PAPER, to_mode=TradingMode.SHADOW,
        actor="u", reason="regress",
        transitioned_at=re_entry, override_used=False,
        mode_started_at_after_transition=re_entry,
    )
    latest = await repo.latest_into(TradingMode.SHADOW)
    assert latest is not None
    assert latest.transitioned_at == re_entry


@pytest.mark.asyncio
async def test_list_forces_in_window(
    repo: ModeTransitionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Returns only override_used=True transitions by the given actor in window."""
    now = _now()
    # Within window, force.
    await repo.add(
        from_mode=TradingMode.PAPER, to_mode=TradingMode.LIVE,
        actor="user:1", reason="emergency",
        transitioned_at=now - timedelta(days=2),
        override_used=True,
        mode_started_at_after_transition=now - timedelta(days=2),
    )
    # Within window, NOT force.
    await repo.add(
        from_mode=TradingMode.SHADOW, to_mode=TradingMode.PAPER,
        actor="user:1", reason="ok",
        transitioned_at=now - timedelta(days=1),
        override_used=False,
        mode_started_at_after_transition=now - timedelta(days=1),
    )
    # Outside window, force.
    await repo.add(
        from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW,
        actor="user:1", reason="old",
        transitioned_at=now - timedelta(days=10),
        override_used=True,
        mode_started_at_after_transition=now - timedelta(days=10),
    )
    # Within window, force, DIFFERENT actor.
    await repo.add(
        from_mode=TradingMode.SHADOW, to_mode=TradingMode.PAPER,
        actor="user:99", reason="other",
        transitioned_at=now - timedelta(days=1),
        override_used=True,
        mode_started_at_after_transition=now - timedelta(days=1),
    )

    forces = await repo.list_forces_in_window(
        actor="user:1", since=now - timedelta(days=7)
    )
    assert len(forces) == 1
    assert forces[0].reason == "emergency"


@pytest.mark.asyncio
async def test_list_recent_descending_order(
    repo: ModeTransitionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = _now() - timedelta(hours=2)
    for i in range(3):
        await repo.add(
            from_mode=TradingMode.OFF, to_mode=TradingMode.SHADOW,
            actor=f"u{i}", reason=None,
            transitioned_at=base + timedelta(minutes=i * 10),
            override_used=False,
            mode_started_at_after_transition=base + timedelta(minutes=i * 10),
        )
    rows = await repo.list_recent(limit=10)
    assert [r.actor for r in rows] == ["u2", "u1", "u0"]


# ─── ModeService ↔ Repo integration ─────────────────────────────────


@pytest.mark.asyncio
async def test_mode_service_appends_transition_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """End-to-end: ``ModeService.transition_to`` writes the audit row."""
    from sqlalchemy import text  # noqa: PLC0415

    from mib.trading.mode_service import ModeService  # noqa: PLC0415
    from mib.trading.risk.state import TradingStateService  # noqa: PLC0415

    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                "VALUES (1, 0, 0.03, 0.25, NULL, 'off', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )

    repo = ModeTransitionRepository(async_session_factory)
    svc = ModeService(
        session_factory=async_session_factory,
        state_service=TradingStateService(async_session_factory),
        transitions_repo=repo,
    )
    result = await svc.transition_to(
        TradingMode.SHADOW, actor="user:1", reason="bring-up"
    )
    assert result.allowed is True
    assert result.transition_id is not None

    latest = await repo.latest()
    assert latest is not None
    assert latest.id == result.transition_id
    assert latest.from_mode == TradingMode.OFF
    assert latest.to_mode == TradingMode.SHADOW
    assert latest.override_used is False
