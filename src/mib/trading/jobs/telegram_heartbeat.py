"""6-hour Telegram status heartbeat (FASE 13.8).

Fired by APScheduler at ``0 */6 * * *`` UTC. Builds a snapshot of:

- Equity (quote currency) + best-effort 6h change
- Open positions count + tickers
- Realised PnL since UTC midnight (D-1 catch-up window)
- Days clean streak
- Modo actual + days in mode
- Incidents in last 24h
- Próximos eventos macro placeholder (FASE 28 wires real data)

Telegram is best-effort — if the bot isn't running we log INFO and
return; the dead-man heartbeat (FASE 13.7) is the canonical
liveness signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from mib.api.dependencies import (
    get_alerter,
    get_incident_repo,
    get_mode_service,
    get_portfolio_state,
    get_trade_repository,
)
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.observability.clean_streak import compute_days_clean_streak
from mib.observability.scheduler_health import get_scheduler_health
from mib.trading.mode_guards import days_in_current_mode


async def telegram_heartbeat_job() -> None:
    """One tick of the 6h status heartbeat. Never raises."""
    # Mark scheduler liveness even if the snapshot build fails.
    get_scheduler_health().mark_tick()
    try:
        message = await _build_message()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_heartbeat: build failed: {}", exc)
        return
    try:
        await get_alerter().alert(message)
    except Exception as exc:  # noqa: BLE001
        logger.info("telegram_heartbeat: send failed: {}", exc)


# ─── Message builder ────────────────────────────────────────────────


async def _build_message() -> str:
    portfolio = await _safe_portfolio_snapshot()
    open_count, tickers = await _count_open_trades()
    realized_pnl_d1 = await _realized_pnl_since_midnight()
    streak = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    mode = await _current_mode()
    days_in_mode_int = await days_in_current_mode(
        mode, async_session_factory
    )
    incidents_24h = await _count_incidents_24h()

    equity_str = (
        f"{portfolio.equity_quote}" if portfolio is not None else "n/a"
    )
    source_str = portfolio.source if portfolio is not None else "n/a"
    tickers_str = ", ".join(tickers) if tickers else "(ninguna)"

    return (
        "📊 <b>MIB Heartbeat</b>\n"
        f"  equity: <code>{equity_str}</code> "
        f"(<i>{source_str}</i>)\n"
        f"  posiciones abiertas: <code>{open_count}</code> "
        f"<i>{tickers_str}</i>\n"
        f"  PnL realised D-1: <code>{realized_pnl_d1}</code>\n"
        f"  días limpios: <code>{streak}</code>\n"
        f"  modo: <code>{mode.value}</code> "
        f"(día <code>{days_in_mode_int}</code>)\n"
        f"  incidentes 24h: <code>{incidents_24h}</code>\n"
        "  próximos eventos macro: <i>(placeholder, FASE 28)</i>"
    )


# ─── Best-effort data sources ───────────────────────────────────────


async def _safe_portfolio_snapshot():  # type: ignore[no-untyped-def]
    try:
        return await get_portfolio_state().snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat: portfolio snapshot failed: {}", exc)
        return None


async def _count_open_trades() -> tuple[int, list[str]]:
    try:
        trades = await get_trade_repository().list_open()
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat: list_open failed: {}", exc)
        return 0, []
    return len(trades), sorted({t.ticker for t in trades})


async def _realized_pnl_since_midnight() -> Decimal:
    """Sum trades.realized_pnl_quote where closed_at >= UTC midnight."""
    midnight = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    try:
        async with async_session_factory() as session:
            stmt = text(
                "SELECT COALESCE(SUM(realized_pnl_quote), 0) "
                "FROM trades WHERE closed_at >= :midnight"
            )
            res = await session.execute(stmt, {"midnight": midnight})
            value = res.scalar()
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat: D-1 pnl query failed: {}", exc)
        return Decimal(0)
    return Decimal(str(value)) if value is not None else Decimal(0)


async def _current_mode():  # type: ignore[no-untyped-def]
    from mib.trading.mode import TradingMode  # noqa: PLC0415

    try:
        return await get_mode_service().get_current()
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat: mode read failed: {}", exc)
        return TradingMode.OFF


async def _count_incidents_24h() -> int:
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
    try:
        rows = await get_incident_repo().list_recent(since=since, limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.debug("heartbeat: incident count failed: {}", exc)
        return 0
    return len(rows)
