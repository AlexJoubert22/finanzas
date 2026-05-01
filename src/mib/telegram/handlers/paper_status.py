"""``/paper_status`` Telegram command (PAPER prep).

Operator-only snapshot of the PAPER-mode validation run. Aggregates
mode + capital + cumulative PnL + win-rate + days-in-PAPER + trades-
in-PAPER + (optional) realised Sharpe, plus the gate to next mode
(``SEMI_AUTO``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_mode_service,
    get_portfolio_state,
)
from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.operations.preflight import MIN_DAYS_IN_PAPER, MIN_TRADES_IN_PAPER
from mib.telegram.formatters import esc
from mib.trading.mode import TradingMode
from mib.trading.mode_guards import (
    closed_trades_in_mode,
    days_in_current_mode,
)

#: Minimum closed trades before realised Sharpe is meaningful. Below
#: this the ratio is too noisy to surface — we show "n/a" instead.
SHARPE_MIN_TRADES: int = 20


@dataclass(frozen=True)
class PaperStatusSnapshot:
    """Aggregate read for ``/paper_status``. Pure data — no Telegram."""

    mode: TradingMode
    baseline_quote: Decimal
    equity_quote: Decimal | None
    cumulative_pnl: Decimal
    days_in_paper: int
    closed_trades: int
    wins: int
    losses: int
    realized_sharpe: float | None
    days_to_next_threshold: int
    trades_to_next_threshold: int

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided > 0 else None

    @property
    def can_advance_to_semi_auto(self) -> bool:
        return (
            self.days_in_paper >= MIN_DAYS_IN_PAPER
            and self.closed_trades >= MIN_TRADES_IN_PAPER
        )


async def build_paper_snapshot(
    *, session_factory: async_sessionmaker[AsyncSession]
) -> PaperStatusSnapshot:
    """Compose the snapshot from existing services + a single trades query."""
    settings = get_settings()
    mode = await _safe_current_mode()
    days = await days_in_current_mode(TradingMode.PAPER, session_factory)
    closed = await closed_trades_in_mode(TradingMode.PAPER, session_factory)

    pnl_total, wins, losses, sharpe = await _aggregate_paper_pnls(
        session_factory
    )

    equity: Decimal | None
    try:
        snap = await get_portfolio_state().snapshot()
        equity = snap.equity_quote
    except Exception as exc:  # noqa: BLE001
        logger.debug("paper_status: portfolio snapshot failed: {}", exc)
        equity = None

    return PaperStatusSnapshot(
        mode=mode,
        baseline_quote=settings.paper_initial_capital_quote,
        equity_quote=equity,
        cumulative_pnl=pnl_total,
        days_in_paper=days,
        closed_trades=closed,
        wins=wins,
        losses=losses,
        realized_sharpe=sharpe,
        days_to_next_threshold=max(MIN_DAYS_IN_PAPER - days, 0),
        trades_to_next_threshold=max(MIN_TRADES_IN_PAPER - closed, 0),
    )


def render_paper_snapshot(snap: PaperStatusSnapshot) -> str:
    """Render the snapshot as a Telegram HTML message."""
    if snap.mode != TradingMode.PAPER:
        header = (
            "⚠️ <b>/paper_status</b> — modo actual NO es PAPER\n"
            f"  modo: <code>{snap.mode.value}</code>\n"
            "<i>El comando muestra contexto de validación PAPER aunque "
            "estemos fuera. Los contadores se refieren a la última "
            "ventana PAPER.</i>\n"
        )
    else:
        header = "🎮 <b>/paper_status</b>\n"

    equity_str = (
        f"{snap.equity_quote}" if snap.equity_quote is not None else "n/a"
    )
    pnl_marker = "🟢" if snap.cumulative_pnl >= 0 else "🔴"
    pnl_pct = ""
    if snap.baseline_quote > 0:
        pct = (snap.cumulative_pnl / snap.baseline_quote) * Decimal(100)
        pnl_pct = f" ({pct.quantize(Decimal('0.01'))}%)"
    win_rate_str = (
        "n/a" if snap.win_rate is None else f"{snap.win_rate * 100:.1f}%"
    )
    if snap.realized_sharpe is None:
        sharpe_str = (
            f"n/a (need ≥{SHARPE_MIN_TRADES} closed trades, "
            f"have {snap.closed_trades})"
        )
    else:
        sharpe_str = f"{snap.realized_sharpe:.2f}"

    if snap.can_advance_to_semi_auto:
        next_mode = (
            "✅ <b>SEMI_AUTO desbloqueado</b> "
            f"({snap.days_in_paper}d, {snap.closed_trades} trades cerrados)"
        )
    else:
        next_mode = (
            f"🔒 SEMI_AUTO bloqueado: faltan "
            f"<code>{snap.days_to_next_threshold}</code>d y "
            f"<code>{snap.trades_to_next_threshold}</code> trades cerrados "
            f"(requisito: {MIN_DAYS_IN_PAPER}d + {MIN_TRADES_IN_PAPER} trades)"
        )

    return (
        f"{header}"
        f"  capital baseline: <code>{snap.baseline_quote}</code> USDT\n"
        f"  equity actual: <code>{equity_str}</code>\n"
        f"  PnL PAPER acumulado: {pnl_marker} <code>{snap.cumulative_pnl}</code>"
        f"{pnl_pct}\n"
        f"  días en PAPER: <code>{snap.days_in_paper}</code>\n"
        f"  trades cerrados: <code>{snap.closed_trades}</code> "
        f"(W:<code>{snap.wins}</code> L:<code>{snap.losses}</code>)\n"
        f"  win-rate: <code>{win_rate_str}</code>\n"
        f"  realised Sharpe: <code>{sharpe_str}</code>\n"
        f"  próximo modo: {next_mode}"
    )


async def paper_status_cmd(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    try:
        snap = await build_paper_snapshot(
            session_factory=async_session_factory
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/paper_status crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/paper_status falló:</b> {esc(str(exc))}"
        )
        return
    await update.message.reply_html(render_paper_snapshot(snap))


# ─── Internal helpers ────────────────────────────────────────────────


async def _safe_current_mode() -> TradingMode:
    try:
        return await get_mode_service().get_current()
    except Exception as exc:  # noqa: BLE001
        logger.debug("paper_status: mode read failed: {}", exc)
        return TradingMode.OFF


async def _aggregate_paper_pnls(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Decimal, int, int, float | None]:
    """Return (cumulative_pnl, wins, losses, sharpe) over PAPER trades.

    A "PAPER trade" is one whose ``closed_at`` falls inside any
    ``[transition_into_PAPER, transition_out_of_PAPER)`` window. We
    pull all closed trades and then filter in Python — small N, simple
    code path.
    """
    windows = await _paper_windows(session_factory)
    if not windows:
        return Decimal(0), 0, 0, None

    pnls: list[Decimal] = []
    wins = losses = 0
    async with session_factory() as session:
        stmt = text(
            "SELECT realized_pnl_quote, closed_at FROM trades "
            "WHERE status='closed' AND closed_at IS NOT NULL "
            "ORDER BY closed_at"
        )
        rows = (await session.execute(stmt)).fetchall()

    for raw_pnl, raw_closed_at in rows:
        closed_at = _coerce_datetime(raw_closed_at)
        if closed_at is None or not _in_any_window(closed_at, windows):
            continue
        if raw_pnl is None:
            continue
        v = Decimal(str(raw_pnl))
        pnls.append(v)
        if v > 0:
            wins += 1
        elif v < 0:
            losses += 1

    cumulative = sum(pnls, Decimal(0))
    sharpe = _realized_sharpe(pnls) if len(pnls) >= SHARPE_MIN_TRADES else None
    return cumulative, wins, losses, sharpe


async def _paper_windows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[tuple[datetime, datetime]]:
    from datetime import UTC  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from mib.db.models import ModeTransitionRow  # noqa: PLC0415

    async with session_factory() as session:
        stmt = (
            select(
                ModeTransitionRow.to_mode,
                ModeTransitionRow.mode_started_at_after_transition,
                ModeTransitionRow.transitioned_at,
            )
            .order_by(ModeTransitionRow.transitioned_at.asc())
        )
        rows = (await session.execute(stmt)).all()

    windows: list[tuple[datetime, datetime]] = []
    current_start: datetime | None = None
    for to_mode, started_at, transitioned_at in rows:
        if to_mode == TradingMode.PAPER.value:
            current_start = started_at
        elif current_start is not None:
            windows.append((current_start, transitioned_at))
            current_start = None
    if current_start is not None:
        windows.append((current_start, datetime.now(UTC).replace(tzinfo=None)))
    return windows


def _in_any_window(
    when: datetime, windows: list[tuple[datetime, datetime]]
) -> bool:
    return any(start <= when < end for start, end in windows)


def _coerce_datetime(value: object) -> datetime | None:
    """SQLite text() queries return TEXT for datetime columns; ORM
    queries return :class:`datetime`. Normalise to datetime so window
    comparisons work either way.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _realized_sharpe(pnls: list[Decimal]) -> float | None:
    """Per-trade realised Sharpe = mean / stdev. Annualisation skipped:
    PAPER cycle isn't long enough for an annual figure to be honest.
    """
    if len(pnls) < 2:
        return None
    floats = [float(p) for p in pnls]
    mean = sum(floats) / len(floats)
    variance = sum((x - mean) ** 2 for x in floats) / (len(floats) - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return None
    return mean / stdev
