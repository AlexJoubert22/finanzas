"""Daily MIB report — fires at 06:00 UTC (≈ 08:00 Madrid).

Spans the previous calendar day in UTC ([yesterday 00:00, today 00:00)).
Sent via the Telegram alerter; falls back to a NullAlerter no-op if
the bot isn't running. Like the 6h heartbeat (FASE 13.8), the job is
best-effort — partial data is preferred over silent failure.

Reports:

- Day PnL (sum of ``trades.realized_pnl_quote`` closed yesterday)
- Day trade count + win/loss breakdown
- Week-to-date PnL (rolling 7d)
- Open positions snapshot
- Current mode + days in mode
- Days clean streak
- Incidents in last 24h
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.api.dependencies import (
    get_alerter,
    get_incident_repo,
    get_mode_service,
    get_portfolio_state,
    get_trade_repository,
)
from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.observability.clean_streak import compute_days_clean_streak
from mib.observability.scheduler_health import get_scheduler_health
from mib.trading.mode import TradingMode
from mib.trading.mode_guards import days_in_current_mode


async def daily_report_job() -> None:
    """One tick of the 08:00 Madrid daily report. Never raises."""
    get_scheduler_health().mark_tick()
    try:
        message = await build_daily_report(
            session_factory=async_session_factory
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_report: build failed: {}", exc)
        return
    try:
        await get_alerter().alert(message)
    except Exception as exc:  # noqa: BLE001
        logger.info("daily_report: send failed: {}", exc)


async def build_daily_report(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> str:
    """Pure(ish) message builder. ``now`` injectable for tests."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_ago = today_start - timedelta(days=7)

    day_stats = await _trade_stats_in_window(
        session_factory, since=yesterday_start, until=today_start
    )
    week_pnl = await _realized_pnl_in_window(
        session_factory, since=week_ago, until=today_start
    )
    open_count, open_tickers = await _count_open_trades()
    portfolio_summary = await _safe_portfolio_summary()
    mode = await _current_mode()
    days_in_mode_int = await days_in_current_mode(mode, session_factory)
    streak = await compute_days_clean_streak(session_factory=session_factory)
    incidents_24h = await _count_incidents_24h(now=now)

    tickers_str = ", ".join(open_tickers) if open_tickers else "(ninguna)"
    pnl_marker = "🟢" if day_stats.pnl >= 0 else "🔴"
    week_marker = "🟢" if week_pnl >= 0 else "🔴"
    win_rate = (
        f"{(day_stats.wins / day_stats.trades * 100):.1f}%"
        if day_stats.trades > 0
        else "n/a"
    )

    settings = get_settings()
    is_paper = mode == TradingMode.PAPER
    baseline = settings.paper_initial_capital_quote if is_paper else None
    paper_header = (
        f"🎮 <b>PAPER MODE</b> — Capital virtual baseline: "
        f"<code>{baseline}</code> USDT\n"
        if is_paper
        else ""
    )
    pnl_pct_str = ""
    week_pnl_pct_str = ""
    drawdown_str = ""
    if baseline is not None and baseline > 0:
        day_pct = (day_stats.pnl / baseline) * Decimal(100)
        week_pct = (week_pnl / baseline) * Decimal(100)
        pnl_pct_str = f" ({day_pct.quantize(Decimal('0.01'))}%)"
        week_pnl_pct_str = f" ({week_pct.quantize(Decimal('0.01'))}%)"
        # DD vs baseline: only meaningful when current equity is known
        # and below baseline.
        equity = await _safe_equity_quote()
        if equity is not None:
            dd_abs = baseline - equity
            if dd_abs > 0:
                dd_pct = (dd_abs / baseline) * Decimal(100)
                drawdown_str = (
                    f"  drawdown vs baseline: <code>"
                    f"{dd_abs.quantize(Decimal('0.01'))}</code> "
                    f"(<code>{dd_pct.quantize(Decimal('0.01'))}%</code>)\n"
                )

    return (
        f"{paper_header}"
        "🌅 <b>MIB Daily Report</b> "
        f"<i>(D-1: {yesterday_start.date()})</i>\n"
        f"  PnL día: {pnl_marker} <code>{day_stats.pnl}</code>"
        f"{pnl_pct_str}\n"
        f"  trades: <code>{day_stats.trades}</code> "
        f"(W:<code>{day_stats.wins}</code> "
        f"L:<code>{day_stats.losses}</code> "
        f"BE:<code>{day_stats.breakevens}</code>) "
        f"win-rate: <code>{win_rate}</code>\n"
        f"  PnL 7d: {week_marker} <code>{week_pnl}</code>"
        f"{week_pnl_pct_str}\n"
        f"  posiciones abiertas: <code>{open_count}</code> "
        f"<i>{tickers_str}</i>\n"
        f"  equity: <code>{portfolio_summary}</code>\n"
        f"{drawdown_str}"
        f"  modo: <code>{mode.value}</code> "
        f"(día <code>{days_in_mode_int}</code>)\n"
        f"  días limpios: <code>{streak}</code>\n"
        f"  incidentes 24h: <code>{incidents_24h}</code>"
    )


# ─── Stats helpers ────────────────────────────────────────────────────


class _DayStats:
    """Tiny container so the builder reads cleanly."""

    __slots__ = ("breakevens", "losses", "pnl", "trades", "wins")

    def __init__(
        self,
        *,
        pnl: Decimal,
        trades: int,
        wins: int,
        losses: int,
        breakevens: int,
    ) -> None:
        self.pnl = pnl
        self.trades = trades
        self.wins = wins
        self.losses = losses
        self.breakevens = breakevens


async def _trade_stats_in_window(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: datetime,
    until: datetime,
) -> _DayStats:
    """Aggregate closed-yesterday trades into a :class:`_DayStats`."""
    try:
        async with session_factory() as session:
            stmt = text(
                "SELECT realized_pnl_quote FROM trades "
                "WHERE closed_at >= :since AND closed_at < :until"
            )
            result = await session.execute(
                stmt, {"since": since, "until": until}
            )
            rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: trade stats query failed: {}", exc)
        return _DayStats(
            pnl=Decimal(0), trades=0, wins=0, losses=0, breakevens=0
        )

    pnl = Decimal(0)
    wins = losses = breakevens = 0
    for (raw,) in rows:
        if raw is None:
            breakevens += 1
            continue
        value = Decimal(str(raw))
        pnl += value
        if value > 0:
            wins += 1
        elif value < 0:
            losses += 1
        else:
            breakevens += 1
    return _DayStats(
        pnl=pnl,
        trades=len(rows),
        wins=wins,
        losses=losses,
        breakevens=breakevens,
    )


async def _realized_pnl_in_window(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since: datetime,
    until: datetime,
) -> Decimal:
    try:
        async with session_factory() as session:
            stmt = text(
                "SELECT COALESCE(SUM(realized_pnl_quote), 0) FROM trades "
                "WHERE closed_at >= :since AND closed_at < :until"
            )
            result = await session.execute(
                stmt, {"since": since, "until": until}
            )
            value = result.scalar()
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: window pnl query failed: {}", exc)
        return Decimal(0)
    return Decimal(str(value)) if value is not None else Decimal(0)


async def _count_open_trades() -> tuple[int, list[str]]:
    try:
        trades = await get_trade_repository().list_open()
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: list_open failed: {}", exc)
        return 0, []
    return len(trades), sorted({t.ticker for t in trades})


async def _safe_portfolio_summary() -> str:
    try:
        snap = await get_portfolio_state().snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: portfolio snapshot failed: {}", exc)
        return "n/a"
    return f"{snap.equity_quote} ({snap.source})"


async def _safe_equity_quote() -> Decimal | None:
    try:
        snap = await get_portfolio_state().snapshot()
    except Exception:  # noqa: BLE001
        return None
    return snap.equity_quote


async def _current_mode() -> Any:  # TradingMode is imported lazily.
    from mib.trading.mode import TradingMode  # noqa: PLC0415

    try:
        return await get_mode_service().get_current()
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: mode read failed: {}", exc)
        return TradingMode.OFF


async def _count_incidents_24h(*, now: datetime) -> int:
    since = now - timedelta(hours=24)
    try:
        rows = await get_incident_repo().list_recent(since=since, limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.debug("daily_report: incident count failed: {}", exc)
        return 0
    return len(rows)
