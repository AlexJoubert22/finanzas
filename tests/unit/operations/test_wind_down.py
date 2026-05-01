"""Tests for the FASE 14.5 /wind_down + /shutdown flow."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from mib.db.models import WindDownStateRow
from mib.db.session import async_session_factory
from mib.operations.wind_down import (
    MIN_WINDDOWN_REASON_LEN,
    WindDownService,
)
from mib.trading.risk.state import TradingStateService
from mib.trading.trade_repo import TradeRepository


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_state(enabled: int = 1) -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                f"VALUES (1, {enabled}, 0.03, 0.25, NULL, 'paper', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


async def _seed_open_trade(ticker: str = "BTC/USDT") -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO signals "
                "(ticker, side, strength, timeframe, entry_low, entry_high, "
                " invalidation, target_1, target_2, rationale, indicators_json, "
                " generated_at, strategy_id, status, status_updated_at) "
                "VALUES (:ticker, 'long', 0.7, '1h', 100, 101, 97, 103, 109, "
                " 'seed', '{}', CURRENT_TIMESTAMP, 'scanner.oversold.v1', "
                " 'pending', CURRENT_TIMESTAMP)"
            ),
            {"ticker": ticker},
        )
        sid = (
            await session.execute(text("SELECT last_insert_rowid()"))
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO trades "
                "(signal_id, ticker, side, size, entry_price, "
                " stop_loss_price, opened_at, status, "
                " realized_pnl_quote, fees_paid_quote, exchange_id) "
                "VALUES (:sid, :ticker, 'long', 0.001, 60000, 58800, "
                " CURRENT_TIMESTAMP, 'open', 0, 0, 'test-ex-id')"
            ),
            {"sid": sid, "ticker": ticker},
        )


def _service() -> WindDownService:
    return WindDownService(
        session_factory=async_session_factory,
        state_service=TradingStateService(async_session_factory),
        trade_repo=TradeRepository(async_session_factory),
    )


# ─── start() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_short_reason_rejected(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    result = await _service().start(actor="user:1", reason="too short")
    assert result.accepted is False
    assert result.reason is not None
    assert "reason_too_short" in result.reason
    # No row was written.
    async with async_session_factory() as session:
        rows = (await session.scalars(select(WindDownStateRow))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_start_with_open_position_records_row_and_disables(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_trade()
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    result = await _service().start(actor="user:1", reason=reason)
    assert result.accepted is True
    assert result.wind_down_id is not None
    assert result.positions_at_start == 1

    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(WindDownStateRow).where(
                    WindDownStateRow.id == result.wind_down_id
                )
            )
        ).first()
    assert row is not None
    assert row.started_by == "wind_down:user:1"
    assert row.completed_at is None  # still in flight
    assert row.positions_at_start == 1
    assert row.positions_remaining_last_check == 1

    # trading_state.enabled flipped off.
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False


@pytest.mark.asyncio
async def test_start_with_zero_positions_completes_immediately(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    result = await _service().start(actor="user:1", reason=reason)
    assert result.accepted is True
    assert result.positions_at_start == 0

    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(WindDownStateRow).where(
                    WindDownStateRow.id == result.wind_down_id
                )
            )
        ).first()
    assert row is not None
    assert row.completed_at is not None
    assert row.positions_remaining_last_check == 0


@pytest.mark.asyncio
async def test_start_refuses_when_already_in_progress(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_trade()
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    first = await _service().start(actor="user:1", reason=reason)
    assert first.accepted is True

    second = await _service().start(actor="user:2", reason=reason)
    assert second.accepted is False
    assert second.reason == "already_in_progress"
    assert second.wind_down_id == first.wind_down_id


@pytest.mark.asyncio
async def test_shutdown_kind_recorded_in_audit_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_trade()
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    result = await _service().start(
        actor="user:7", reason=reason, kind="shutdown"
    )
    assert result.accepted is True
    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(WindDownStateRow).where(
                    WindDownStateRow.id == result.wind_down_id
                )
            )
        ).first()
    assert row is not None
    assert row.started_by == "shutdown:user:7"


# ─── tick() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_no_active_returns_none(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    result = await _service().tick()
    assert result is None


@pytest.mark.asyncio
async def test_tick_updates_remaining_count(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_trade("BTC/USDT")
    await _seed_open_trade("ETH/USDT")
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    started = await _service().start(actor="user:1", reason=reason)
    assert started.positions_at_start == 2

    # Close one trade by hand.
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE trades SET status='closed', "
                "closed_at=CURRENT_TIMESTAMP "
                "WHERE ticker='BTC/USDT'"
            )
        )

    result = await _service().tick()
    assert result is not None
    assert result.positions_remaining == 1
    assert result.completed is False

    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(WindDownStateRow).where(
                    WindDownStateRow.id == started.wind_down_id
                )
            )
        ).first()
    assert row is not None
    assert row.positions_remaining_last_check == 1
    assert row.last_check_at is not None
    assert row.completed_at is None


@pytest.mark.asyncio
async def test_tick_auto_completes_when_zero(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    await _seed_open_trade("BTC/USDT")
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    started = await _service().start(actor="user:1", reason=reason)

    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE trades SET status='closed', "
                "closed_at=CURRENT_TIMESTAMP"
            )
        )

    result = await _service().tick()
    assert result is not None
    assert result.positions_remaining == 0
    assert result.completed is True

    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(WindDownStateRow).where(
                    WindDownStateRow.id == started.wind_down_id
                )
            )
        ).first()
    assert row is not None
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_tick_after_complete_returns_none(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """A tick on a completed wind-down is a no-op."""
    await _seed_state()
    reason = "x" * MIN_WINDDOWN_REASON_LEN
    # Start with zero positions -> completes immediately.
    result = await _service().start(actor="user:1", reason=reason)
    assert result.accepted is True
    tick = await _service().tick()
    assert tick is None
