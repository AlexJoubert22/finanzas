"""Tests for the append-only ``SignalRepository.transition`` helper.

Per ROADMAP.md Parte 0 mandate, ``transition`` is the only allowed
mutation API for ``signals.status``. These tests cover:

- Creation writes a 'created' event with ``from_status=None``.
- Consuming a pending signal writes the correct event.
- Consecutive transitions append (the table is append-only).
- Audit trail (``actor``, ``reason``, ``metadata_json``) populated.
- Chronological ordering.
- ``expected_from_status`` mismatch raises ``StaleSignalStateError``.
- Unknown signal id returns ``None``.
- Invalid status values raise ``ValueError``.
- The single-transaction guarantee: events and cache stay in sync
  even when called concurrently.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from mib.db.session import async_session_factory
from mib.trading.signal_repo import (
    SignalRepository,
    StaleSignalStateError,
)
from mib.trading.signals import Signal


def _signal(*, ticker: str = "BTC/USDT") -> Signal:
    return Signal(
        ticker=ticker,
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


@pytest.fixture
def repo() -> SignalRepository:
    return SignalRepository(async_session_factory)


@pytest.mark.asyncio
async def test_add_writes_created_event_with_null_from_status(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    events = await repo.list_events(persisted.id)
    assert len(events) == 1
    e = events[0]
    assert e.from_status is None
    assert e.to_status == "pending"
    assert e.event_type == "created"
    assert e.actor == "system"


@pytest.mark.asyncio
async def test_consume_pending_writes_correct_event(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    result = await repo.transition(
        persisted.id,
        "consumed",
        actor="user:42",
        event_type="approved",
        reason="operator-approved",
    )
    assert result is not None
    assert result.status == "consumed"

    events = await repo.list_events(persisted.id)
    assert len(events) == 2
    transition_event = events[1]
    assert transition_event.from_status == "pending"
    assert transition_event.to_status == "consumed"
    assert transition_event.event_type == "approved"
    assert transition_event.actor == "user:42"
    assert transition_event.reason == "operator-approved"


@pytest.mark.asyncio
async def test_consecutive_transitions_append_not_overwrite(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Even if we transition pending → consumed → reconciled, every
    step adds a row. Append-only by construction.
    """
    persisted = await repo.add(_signal())
    await repo.transition(
        persisted.id, "consumed", actor="user:1", event_type="approved"
    )
    # A reconciler later "reverts" the consumed back to pending (hypothetical).
    # The point is: the audit trail should preserve all three events.
    await repo.transition(
        persisted.id,
        "expired",
        actor="job:reconcile",
        event_type="reconciled",
        reason="post-mortem cleanup",
    )

    events = await repo.list_events(persisted.id)
    assert len(events) == 3
    assert [e.event_type for e in events] == ["created", "approved", "reconciled"]
    assert [e.to_status for e in events] == ["pending", "consumed", "expired"]


@pytest.mark.asyncio
async def test_metadata_json_persisted(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    await repo.transition(
        persisted.id,
        "consumed",
        actor="user:42",
        event_type="approved",
        metadata={"sized_amount_eur": 50.0, "gates_passed": ["kill", "dd"]},
    )
    events = await repo.list_events(persisted.id)
    payload = events[1].metadata_json or {}
    assert payload["sized_amount_eur"] == 50.0
    assert payload["gates_passed"] == ["kill", "dd"]


@pytest.mark.asyncio
async def test_events_returned_in_chronological_order(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    await repo.transition(
        persisted.id, "consumed", actor="user:1", event_type="approved"
    )
    events = await repo.list_events(persisted.id)
    # 'created' first, then 'approved'.
    timestamps = [e.created_at for e in events]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_expected_from_status_mismatch_raises_stale_error(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    # First actor consumes it.
    await repo.transition(
        persisted.id, "consumed", actor="user:1", event_type="approved"
    )
    # Second actor expects to find it pending — gets stale error instead.
    with pytest.raises(StaleSignalStateError) as excinfo:
        await repo.transition(
            persisted.id,
            "cancelled",
            actor="user:2",
            event_type="cancelled",
            expected_from_status="pending",
        )
    assert excinfo.value.expected == "pending"
    assert excinfo.value.actual == "consumed"


@pytest.mark.asyncio
async def test_transition_unknown_id_returns_none(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    result = await repo.transition(
        99_999, "consumed", actor="user:1", event_type="approved"
    )
    assert result is None


@pytest.mark.asyncio
async def test_transition_rejects_invalid_to_status(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    with pytest.raises(ValueError, match="invalid SignalStatus"):
        await repo.transition(
            persisted.id,
            "approved",  # type: ignore[arg-type]
            actor="user:1",
            event_type="approved",
        )


@pytest.mark.asyncio
async def test_transition_rejects_invalid_expected_from_status(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    with pytest.raises(ValueError, match="invalid expected"):
        await repo.transition(
            persisted.id,
            "consumed",
            actor="user:1",
            event_type="approved",
            expected_from_status="approved",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_concurrent_transitions_only_one_wins(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Two coroutines try to consume the same pending signal. With
    expected_from_status='pending' guard, only the first one writes
    the consumed event; the second sees status='consumed' and raises
    StaleSignalStateError. Total events: 1 created + 1 consumed = 2.
    """
    persisted = await repo.add(_signal())

    async def attempt_consume(actor: str) -> str:
        try:
            await repo.transition(
                persisted.id,
                "consumed",
                actor=actor,
                event_type="approved",
                expected_from_status="pending",
            )
        except StaleSignalStateError:
            return "stale"
        return "ok"

    results = await asyncio.gather(
        attempt_consume("user:A"),
        attempt_consume("user:B"),
    )
    # Exactly one should succeed.
    assert sorted(results) == ["ok", "stale"]
    events = await repo.list_events(persisted.id)
    # 1 created + exactly 1 consumed.
    assert len(events) == 2
    assert events[1].event_type == "approved"
