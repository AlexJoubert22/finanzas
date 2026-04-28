"""Tests for :class:`TradingStateService` and the singleton row contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from mib.db.session import async_session_factory
from mib.trading.risk.state import TradingStateService


async def _seed() -> None:
    """Insert the singleton row exactly like the seed migration does."""
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO trading_state "
                    "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                    " killed_until, last_modified_at, last_modified_by) "
                    "VALUES (1, 0, 0.03, 0.25, NULL, "
                    " CURRENT_TIMESTAMP, 'test:fixture')"
                )
            )


@pytest.fixture
def state_service() -> TradingStateService:
    return TradingStateService(async_session_factory)


@pytest.mark.asyncio
async def test_get_returns_seeded_defaults(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    await _seed()
    snap = await state_service.get()
    assert snap.enabled is False
    assert snap.daily_dd_max_pct == pytest.approx(0.03)
    assert snap.total_dd_max_pct == pytest.approx(0.25)
    assert snap.killed_until is None
    assert snap.last_modified_by == "test:fixture"


@pytest.mark.asyncio
async def test_get_raises_when_singleton_missing(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    with pytest.raises(RuntimeError, match="trading_state singleton"):
        await state_service.get()


@pytest.mark.asyncio
async def test_update_changes_field_and_audit_metadata(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    await _seed()
    before = await state_service.get()
    after = await state_service.update(actor="user:42", enabled=True)
    assert after.enabled is True
    assert after.last_modified_by == "user:42"
    # last_modified_at advanced.
    assert after.last_modified_at >= before.last_modified_at


@pytest.mark.asyncio
async def test_update_with_killed_until(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    await _seed()
    cutoff = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    after = await state_service.update(
        actor="gate:daily_drawdown", killed_until=cutoff
    )
    assert after.killed_until == cutoff


@pytest.mark.asyncio
async def test_update_rejects_unknown_keys(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    await _seed()
    with pytest.raises(ValueError, match="unknown trading_state keys"):
        await state_service.update(actor="user:1", foo=True)


@pytest.mark.asyncio
async def test_update_requires_actor(
    state_service: TradingStateService, fresh_db: None  # noqa: ARG001
) -> None:
    await _seed()
    with pytest.raises(ValueError, match="actor"):
        await state_service.update(actor="", enabled=True)


@pytest.mark.asyncio
async def test_singleton_check_constraint_blocks_second_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """The CHECK(id = 1) constraint must reject any insert with a different id."""
    await _seed()
    with pytest.raises(IntegrityError):
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO trading_state "
                        "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                        " killed_until, last_modified_at, last_modified_by) "
                        "VALUES (2, 0, 0.03, 0.25, NULL, "
                        " CURRENT_TIMESTAMP, 'attacker')"
                    )
                )
