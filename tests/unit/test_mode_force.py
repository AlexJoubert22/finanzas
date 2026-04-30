"""Tests for ``/mode_force`` flow (FASE 10.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from mib.db.session import async_session_factory
from mib.trading.mode import TradingMode
from mib.trading.mode_service import (
    FORCE_RATE_LIMIT_WINDOW,
    MAX_FORCES_PER_WEEK_PER_ACTOR,
    MIN_FORCE_REASON_LEN,
    ForceRateLimitExceededError,
    ForceReasonTooShortError,
    ModeService,
)
from mib.trading.mode_transitions_repo import ModeTransitionRepository
from mib.trading.risk.state import TradingStateService


async def _seed_state(*, mode: str = "paper") -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                f"VALUES (1, 0, 0.03, 0.25, NULL, '{mode}', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


def _service() -> tuple[ModeService, ModeTransitionRepository]:
    repo = ModeTransitionRepository(async_session_factory)
    svc = ModeService(
        session_factory=async_session_factory,
        state_service=TradingStateService(async_session_factory),
        transitions_repo=repo,
    )
    return svc, repo


# ─── Reason length ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_rejects_short_reason(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state(mode="paper")
    svc, _ = _service()
    with pytest.raises(ForceReasonTooShortError) as exc_info:
        await svc.force_transition_to(
            TradingMode.SEMI_AUTO,
            actor="user:1",
            reason="too short",  # 9 chars
        )
    assert exc_info.value.length == 9


@pytest.mark.asyncio
async def test_force_rejects_whitespace_padded_short_reason(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Reason is stripped before length check; pure whitespace fails."""
    await _seed_state(mode="paper")
    svc, _ = _service()
    with pytest.raises(ForceReasonTooShortError):
        await svc.force_transition_to(
            TradingMode.SEMI_AUTO,
            actor="user:1",
            reason="    short    ",
        )


# ─── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_success_persists_override_flag(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Reason >=20 chars + first force -> success, audit row marks override."""
    await _seed_state(mode="paper")
    svc, repo = _service()
    valid_reason = "Emergency bypass for SEMI_AUTO escalation per ops"
    assert len(valid_reason) >= MIN_FORCE_REASON_LEN

    result = await svc.force_transition_to(
        TradingMode.SEMI_AUTO,
        actor="user:1",
        reason=valid_reason,
    )
    assert result.allowed is True
    assert result.from_mode == TradingMode.PAPER
    assert result.to_mode == TradingMode.SEMI_AUTO
    assert result.transition_id is not None

    latest = await repo.latest()
    assert latest is not None
    assert latest.id == result.transition_id
    assert latest.override_used is True
    assert latest.reason == valid_reason


# ─── Rate limit (1 per 7d per actor) ────────────────────────────────


@pytest.mark.asyncio
async def test_force_rate_limit_exceeded_within_window(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Second force inside 7 days for same actor -> rate-limit error."""
    await _seed_state(mode="paper")
    svc, repo = _service()

    # First force: succeeds.
    valid_reason = "Initial force to escalate to SEMI_AUTO under operator review"
    await svc.force_transition_to(
        TradingMode.SEMI_AUTO,
        actor="user:1",
        reason=valid_reason,
    )

    # Now PAPER -> SEMI_AUTO done; revert to PAPER (regression with reason
    # is allowed by guards) so we can attempt another force.
    await svc.transition_to(
        TradingMode.PAPER,
        actor="user:1",
        reason="regression for test",
    )

    # Second force inside the window -> rate-limit blocks.
    with pytest.raises(ForceRateLimitExceededError) as exc_info:
        await svc.force_transition_to(
            TradingMode.SEMI_AUTO,
            actor="user:1",
            reason="Trying again, second forced escalation attempt",
        )
    assert exc_info.value.actor == "user:1"
    assert exc_info.value.window_count == MAX_FORCES_PER_WEEK_PER_ACTOR
    assert exc_info.value.limit == MAX_FORCES_PER_WEEK_PER_ACTOR

    # No new force row created.
    forces = await repo.list_forces_in_window(
        actor="user:1",
        since=datetime.now(UTC).replace(tzinfo=None) - FORCE_RATE_LIMIT_WINDOW,
    )
    assert len(forces) == 1


@pytest.mark.asyncio
async def test_force_rate_limit_independent_per_actor(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Different actors each get their own 1-per-7d budget."""
    await _seed_state(mode="paper")
    svc, _ = _service()

    valid_reason = "Per-operator force budget independence verification path"
    await svc.force_transition_to(
        TradingMode.SEMI_AUTO,
        actor="user:1",
        reason=valid_reason,
    )

    # user:2 hasn't forced yet; they can. Regress first to give them a
    # forward step to take.
    await svc.transition_to(
        TradingMode.PAPER, actor="user:2", reason="for next test"
    )
    result = await svc.force_transition_to(
        TradingMode.SEMI_AUTO,
        actor="user:2",
        reason=valid_reason,
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_old_force_outside_window_doesnt_block(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """A force from 10 days ago doesn't count against today's budget."""
    await _seed_state(mode="paper")
    svc, repo = _service()
    # Manually seed an OLD force row (outside 7-day window).
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=10)
    await repo.add(
        from_mode=TradingMode.PAPER,
        to_mode=TradingMode.SEMI_AUTO,
        actor="user:1",
        reason="historical force outside the window",
        transitioned_at=old,
        override_used=True,
        mode_started_at_after_transition=old,
    )

    # Today's force should succeed.
    valid_reason = "Fresh force inside window after a 10-day-old historical one"
    result = await svc.force_transition_to(
        TradingMode.SEMI_AUTO,
        actor="user:1",
        reason=valid_reason,
    )
    assert result.allowed is True


# ─── Force still surfaces no_op rejection ───────────────────────────


@pytest.mark.asyncio
async def test_force_no_op_transition_rejected(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """force=True still surfaces same-mode rejection (cheap pre-check)."""
    await _seed_state(mode="paper")
    svc, _ = _service()
    valid_reason = "Trying to force a transition that is a no-op anyway sanity"
    result = await svc.force_transition_to(
        TradingMode.PAPER,
        actor="user:1",
        reason=valid_reason,
    )
    assert result.allowed is False
    assert result.reason == "no_op_transition"


# ─── Empty actor still blocked ──────────────────────────────────────


@pytest.mark.asyncio
async def test_force_empty_actor_raises(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_state(mode="paper")
    svc, _ = _service()
    with pytest.raises(ValueError, match="actor must be"):
        await svc.force_transition_to(
            TradingMode.SEMI_AUTO,
            actor="",
            reason="adequately long reason for a forced transition test",
        )
