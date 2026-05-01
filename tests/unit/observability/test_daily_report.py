"""Tests for the FASE 14.4 daily report job."""

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
from mib.trading.jobs import daily_report as dr_mod


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_state(mode: str = "paper") -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                f"VALUES (1, 1, 0.03, 0.25, NULL, '{mode}', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


async def _insert_closed_trade(
    *, pnl: Decimal, closed_at: datetime, ticker: str = "BTC/USDT"
) -> None:
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
                " stop_loss_price, opened_at, closed_at, status, "
                " realized_pnl_quote, fees_paid_quote, exchange_id) "
                "VALUES (:sid, :ticker, 'long', 0.001, 60000, 58800, "
                " :opened, :closed, 'closed', :pnl, 0, 'test-ex-id')"
            ),
            {
                "sid": sid,
                "ticker": ticker,
                "opened": closed_at - timedelta(hours=1),
                "closed": closed_at,
                "pnl": str(pnl),
            },
        )


@pytest.mark.asyncio
async def test_daily_report_renders_full_message(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot-check labels, PnL marker, mode and incident count."""
    await _seed_state()
    repo = CriticalIncidentRepository(async_session_factory)
    await repo.add(
        type_=CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED,
        occurred_at=_now() - timedelta(hours=2),
        auto_detected=False,
    )

    alerter = NullAlerter()
    monkeypatch.setattr(dr_mod, "get_alerter", lambda: alerter)
    await dr_mod.daily_report_job()

    assert len(alerter.recorded) == 1
    msg = alerter.recorded[0]
    assert "MIB Daily Report" in msg
    assert "PnL día" in msg
    assert "trades:" in msg
    assert "PnL 7d" in msg
    assert "posiciones abiertas" in msg
    assert "modo:" in msg
    assert "días limpios" in msg
    assert "incidentes 24h" in msg
    assert "paper" in msg
    assert "incidentes 24h: <code>1</code>" in msg


@pytest.mark.asyncio
async def test_daily_report_aggregates_yesterday_only(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Only trades closed in [yesterday 00:00, today 00:00) are counted."""
    await _seed_state()
    fixed_now = datetime(2026, 5, 1, 8, 0)  # within today 2026-05-01
    yesterday = datetime(2026, 4, 30, 14, 0)
    two_days_ago = datetime(2026, 4, 29, 14, 0)
    today = datetime(2026, 5, 1, 7, 0)

    await _insert_closed_trade(pnl=Decimal("100"), closed_at=yesterday)
    await _insert_closed_trade(pnl=Decimal("-25"), closed_at=yesterday)
    await _insert_closed_trade(pnl=Decimal("999"), closed_at=two_days_ago)
    await _insert_closed_trade(pnl=Decimal("888"), closed_at=today)

    msg = await dr_mod.build_daily_report(
        session_factory=async_session_factory, now=fixed_now
    )
    # Only yesterday's two trades roll up to PnL día = 75.
    assert "<code>75</code>" in msg
    assert "trades: <code>2</code>" in msg
    assert "W:<code>1</code>" in msg
    assert "L:<code>1</code>" in msg


@pytest.mark.asyncio
async def test_daily_report_week_pnl_window_7d(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """7d window covers today_start - 7d to today_start."""
    await _seed_state()
    fixed_now = datetime(2026, 5, 1, 8, 0)
    # Three trades within window, one outside.
    await _insert_closed_trade(
        pnl=Decimal("10"), closed_at=datetime(2026, 4, 25, 12, 0)
    )
    await _insert_closed_trade(
        pnl=Decimal("20"), closed_at=datetime(2026, 4, 28, 12, 0)
    )
    await _insert_closed_trade(
        pnl=Decimal("30"), closed_at=datetime(2026, 4, 30, 12, 0)
    )
    await _insert_closed_trade(
        pnl=Decimal("999"), closed_at=datetime(2026, 4, 23, 12, 0)
    )  # > 7d, excluded
    msg = await dr_mod.build_daily_report(
        session_factory=async_session_factory, now=fixed_now
    )
    assert "PnL 7d: 🟢 <code>60</code>" in msg


@pytest.mark.asyncio
async def test_daily_report_no_trades_shows_zeros(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    msg = await dr_mod.build_daily_report(
        session_factory=async_session_factory
    )
    assert "PnL día: 🟢 <code>0</code>" in msg
    assert "trades: <code>0</code>" in msg
    assert "win-rate: <code>n/a</code>" in msg


@pytest.mark.asyncio
async def test_daily_report_negative_pnl_uses_red_marker(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    fixed_now = datetime(2026, 5, 1, 8, 0)
    yesterday = datetime(2026, 4, 30, 14, 0)
    await _insert_closed_trade(pnl=Decimal("-50"), closed_at=yesterday)
    msg = await dr_mod.build_daily_report(
        session_factory=async_session_factory, now=fixed_now
    )
    assert "PnL día: 🔴 <code>-50</code>" in msg


@pytest.mark.asyncio
async def test_daily_report_swallows_alerter_failure(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_state()

    class _RaisingAlerter:
        async def alert(
            self, text: str, *, parse_mode: str = "HTML"  # noqa: ARG002
        ) -> None:
            raise RuntimeError("telegram down")

    monkeypatch.setattr(dr_mod, "get_alerter", lambda: _RaisingAlerter())
    await dr_mod.daily_report_job()  # must not raise
