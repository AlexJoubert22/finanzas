"""Smoke test for the 6h Telegram heartbeat job (FASE 13.8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from mib.db.session import async_session_factory
from mib.observability.incidents import (
    CriticalIncidentRepository,
    CriticalIncidentType,
)
from mib.trading.alerter import NullAlerter
from mib.trading.jobs import telegram_heartbeat as hb_mod


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_state() -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                "VALUES (1, 1, 0.03, 0.25, NULL, 'paper', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


@pytest.mark.asyncio
async def test_heartbeat_job_sends_message_with_expected_fields(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end smoke: build_message + alert. NullAlerter records."""
    await _seed_state()
    # Seed an incident for the 'incidentes 24h' line.
    repo = CriticalIncidentRepository(async_session_factory)
    await repo.add(
        type_=CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED,
        occurred_at=_now() - timedelta(hours=2),
        auto_detected=False,
    )

    alerter = NullAlerter()
    monkeypatch.setattr(hb_mod, "get_alerter", lambda: alerter)

    await hb_mod.telegram_heartbeat_job()
    assert len(alerter.recorded) == 1
    msg = alerter.recorded[0]
    # Spot-check the spec-mandated field labels.
    assert "MIB Heartbeat" in msg
    assert "equity" in msg
    assert "posiciones abiertas" in msg
    assert "PnL realised" in msg
    assert "días limpios" in msg
    assert "modo" in msg
    assert "incidentes 24h" in msg
    # Mode read from seeded state.
    assert "paper" in msg
    # Incident count 1 (24h window).
    assert "incidentes 24h: <code>1</code>" in msg


@pytest.mark.asyncio
async def test_heartbeat_job_swallows_alerter_failure(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alerter raise must NOT bubble out — telegram is best-effort."""
    await _seed_state()

    class _RaisingAlerter:
        async def alert(
            self, text: str, *, parse_mode: str = "HTML"  # noqa: ARG002
        ) -> None:
            raise RuntimeError("telegram down")

    monkeypatch.setattr(hb_mod, "get_alerter", lambda: _RaisingAlerter())
    # Just shouldn't raise.
    await hb_mod.telegram_heartbeat_job()


@pytest.mark.asyncio
async def test_heartbeat_pnl_query_handles_empty_trades(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trades closed today → PnL D-1 line shows 0."""
    await _seed_state()
    alerter = NullAlerter()
    monkeypatch.setattr(hb_mod, "get_alerter", lambda: alerter)
    await hb_mod.telegram_heartbeat_job()
    msg = alerter.recorded[0]
    assert "PnL realised D-1: <code>0" in msg


@pytest.mark.asyncio
async def test_heartbeat_pnl_query_sums_today_closed_trades(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed trade since UTC midnight is summed in the PnL line."""
    await _seed_state()
    pnl = await hb_mod._realized_pnl_since_midnight()
    assert pnl == Decimal(0)
    # Insert a closed trade with realized_pnl_quote=42 (raw row to
    # avoid the full SignalRow + TradeRepository chain — we're
    # testing the SQL helper, not the trade repo).
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO signals "
                "(ticker, side, strength, timeframe, entry_low, entry_high, "
                " invalidation, target_1, target_2, rationale, indicators_json, "
                " generated_at, strategy_id, status, status_updated_at) "
                "VALUES ('BTC/USDT', 'long', 0.7, '1h', 100, 101, 97, 103, 109, "
                " 'seed', '{}', CURRENT_TIMESTAMP, 'scanner.oversold.v1', "
                " 'pending', CURRENT_TIMESTAMP)"
            )
        )
        sid = (
            await session.execute(text("SELECT last_insert_rowid()"))
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO trades "
                "(signal_id, ticker, side, size, entry_price, "
                " stop_loss_price, opened_at, closed_at, status, "
                " realized_pnl_quote, fees_paid_quote, exchange_id) "
                "VALUES (:sid, 'BTC/USDT', 'long', 0.001, 60000, 58800, "
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'closed', 42, 0, "
                " 'binance_sandbox')"
            ),
            {"sid": sid},
        )
    pnl_after = await hb_mod._realized_pnl_since_midnight()
    assert pnl_after == Decimal(42)
