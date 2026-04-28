"""Tests for the TTL expiration job.

Covers:
- Pending signal with ``expires_at < now`` becomes ``expired``.
- Consumed signal is left alone (terminal state).
- Future ``expires_at`` is left alone.
- The job is idempotent — running twice does not double-write events.
- Audit event has the expected ``actor`` and a reason mentioning TTL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.api.dependencies import get_signal_repository
from mib.trading.expiration import expire_stale_signals_job
from mib.trading.signals import Signal


def _signal(*, generated_at: datetime, timeframe: str = "1h") -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe=timeframe,
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=generated_at,
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


@pytest.mark.asyncio
async def test_pending_with_past_expires_at_becomes_expired(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Generated 5h ago with 1h timeframe + 4 ttl_bars = expired 1h ago."""
    repo = get_signal_repository()
    five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
    persisted = await repo.add(_signal(generated_at=five_hours_ago))
    assert persisted.status == "pending"

    expired = await expire_stale_signals_job()
    assert expired == 1

    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.status == "expired"


@pytest.mark.asyncio
async def test_consumed_signal_is_left_alone(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_signal_repository()
    five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
    persisted = await repo.add(_signal(generated_at=five_hours_ago))
    # Consume it — now its status is no longer 'pending'.
    await repo.transition(
        persisted.id, "consumed", actor="user:test", event_type="approved"
    )

    expired = await expire_stale_signals_job()
    assert expired == 0

    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.status == "consumed"


@pytest.mark.asyncio
async def test_future_expires_at_is_left_alone(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_signal_repository()
    # Generated now → expires 4h from now.
    persisted = await repo.add(_signal(generated_at=datetime.now(UTC)))

    expired = await expire_stale_signals_job()
    assert expired == 0

    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.status == "pending"


@pytest.mark.asyncio
async def test_job_is_idempotent(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Second run on already-expired signals is a no-op (no double events)."""
    repo = get_signal_repository()
    five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
    persisted = await repo.add(_signal(generated_at=five_hours_ago))

    first_pass = await expire_stale_signals_job()
    second_pass = await expire_stale_signals_job()

    assert first_pass == 1
    assert second_pass == 0

    events = await repo.list_events(persisted.id)
    # Exactly: created + expired = 2 events.
    assert len(events) == 2
    assert [e.event_type for e in events] == ["created", "expired"]


@pytest.mark.asyncio
async def test_actor_and_reason_populated(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_signal_repository()
    five_hours_ago = datetime.now(UTC) - timedelta(hours=5)
    persisted = await repo.add(_signal(generated_at=five_hours_ago))

    await expire_stale_signals_job()

    events = await repo.list_events(persisted.id)
    expired_event = events[-1]
    assert expired_event.event_type == "expired"
    assert expired_event.actor == "job:expire_stale_signals"
    assert expired_event.reason is not None
    assert "TTL elapsed" in expired_event.reason
