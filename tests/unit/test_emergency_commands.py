"""Tests for ``/stop``, ``/freeze``, ``/risk`` emergency commands."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from mib.db.session import async_session_factory
from mib.telegram.handlers.emergency import freeze_cmd, risk_cmd, stop_cmd
from mib.trading.risk.state import TradingStateService


def _fake_update(*, telegram_id: int = 42) -> Any:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_id
    update.message = MagicMock()
    update.message.reply_html = AsyncMock()
    return update


def _fake_context(args: list[str] | None = None) -> Any:
    ctx = MagicMock()
    ctx.args = args or []
    return ctx


async def _seed_trading_state(*, enabled: bool = True) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO trading_state "
                    "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                    " killed_until, last_modified_at, last_modified_by) "
                    f"VALUES (1, {1 if enabled else 0}, 0.03, 0.25, "
                    "NULL, CURRENT_TIMESTAMP, 'test:fixture')"
                )
            )


# ─── /stop ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_sets_enabled_false(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_trading_state(enabled=True)
    update = _fake_update()
    ctx = _fake_context(args=["panic", "button"])
    await stop_cmd(update, ctx)

    assert update.message.reply_html.await_count == 1
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False
    assert "user:42" in state.last_modified_by
    assert "panic button" in state.last_modified_by


@pytest.mark.asyncio
async def test_stop_works_without_reason(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_trading_state(enabled=True)
    update = _fake_update()
    await stop_cmd(update, _fake_context())

    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False
    assert "no reason given" in state.last_modified_by


@pytest.mark.asyncio
async def test_stop_idempotent_when_already_off(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Pressing /stop when already off doesn't error — just confirms."""
    await _seed_trading_state(enabled=False)
    update = _fake_update()
    await stop_cmd(update, _fake_context())
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False


# ─── /freeze ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_freeze_sets_enabled_false(fresh_db: None) -> None:  # noqa: ARG001
    await _seed_trading_state(enabled=True)
    update = _fake_update(telegram_id=99)
    await freeze_cmd(update, _fake_context())
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False
    assert "user:99" in state.last_modified_by


@pytest.mark.asyncio
async def test_freeze_message_distinguishes_from_stop(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_trading_state(enabled=True)
    update = _fake_update()
    await freeze_cmd(update, _fake_context())
    msg = update.message.reply_html.await_args[0][0]
    assert "Freeze" in msg
    assert "stops nativos" in msg


# ─── /risk ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_returns_formatted_snapshot(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_trading_state(enabled=True)
    update = _fake_update()
    await risk_cmd(update, _fake_context())
    assert update.message.reply_html.await_count == 1
    msg = update.message.reply_html.await_args[0][0]
    assert "trading_state" in msg
    assert "enabled" in msg
    assert "daily_dd_max_pct" in msg


@pytest.mark.asyncio
async def test_risk_handles_missing_state_gracefully(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """No seed → service.get() raises; /risk reports the error."""
    update = _fake_update()
    await risk_cmd(update, _fake_context())
    msg = update.message.reply_html.await_args[0][0]
    assert "trading_state no disponible" in msg
