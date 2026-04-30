"""Tests for :class:`ModeService` (FASE 10.1)."""

from __future__ import annotations

import pytest

from mib.db.session import async_session_factory
from mib.trading.mode import TradingMode
from mib.trading.mode_service import ModeService
from mib.trading.risk.state import TradingStateService


async def _seed_state(*, mode: str = "off") -> None:
    """Insert the singleton trading_state row with the given mode."""
    from sqlalchemy import text  # noqa: PLC0415

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


def _service() -> ModeService:
    return ModeService(
        session_factory=async_session_factory,
        state_service=TradingStateService(async_session_factory),
    )


@pytest.mark.asyncio
async def test_get_current_default_off(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_state(mode="off")
    assert await _service().get_current() == TradingMode.OFF


@pytest.mark.asyncio
async def test_off_to_shadow_allowed(fresh_db: None) -> None:  # noqa: ARG001
    """OFF → SHADOW is the canonical first step; always permitted."""
    await _seed_state(mode="off")
    service = _service()
    result = await service.transition_to(
        TradingMode.SHADOW, actor="user:1", reason="initial bring-up"
    )
    assert result.allowed is True
    assert result.from_mode == TradingMode.OFF
    assert result.to_mode == TradingMode.SHADOW
    # Persisted: re-reading returns the new mode.
    assert await service.get_current() == TradingMode.SHADOW


@pytest.mark.asyncio
async def test_no_op_transition_rejected(fresh_db: None) -> None:  # noqa: ARG001
    """Same mode → same mode produces no_op_transition rejection."""
    await _seed_state(mode="shadow")
    result = await _service().transition_to(
        TradingMode.SHADOW, actor="user:1"
    )
    assert result.allowed is False
    assert result.reason == "no_op_transition"


@pytest.mark.asyncio
async def test_unknown_persisted_mode_coerces_to_off(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """If a hand-edited DB row carries garbage, get_current returns OFF."""
    await _seed_state(mode="garbage")
    assert await _service().get_current() == TradingMode.OFF


@pytest.mark.asyncio
async def test_actor_required(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_state(mode="off")
    with pytest.raises(ValueError, match="actor must be"):
        await _service().transition_to(TradingMode.SHADOW, actor="")


@pytest.mark.asyncio
async def test_mode_persists_across_service_instances(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Bot reboot simulation: new service instance reads the same mode."""
    await _seed_state(mode="off")
    await _service().transition_to(
        TradingMode.SHADOW, actor="user:1", reason="bring-up"
    )
    # Fresh service instance — reads from DB on every get_current.
    fresh_service = _service()
    assert await fresh_service.get_current() == TradingMode.SHADOW


def test_trading_mode_values_match_spec() -> None:
    """Sanity: enum values are the lowercase strings 10.1 expects."""
    assert TradingMode.OFF.value == "off"
    assert TradingMode.SHADOW.value == "shadow"
    assert TradingMode.PAPER.value == "paper"
    assert TradingMode.SEMI_AUTO.value == "semi_auto"
    assert TradingMode.LIVE.value == "live"
