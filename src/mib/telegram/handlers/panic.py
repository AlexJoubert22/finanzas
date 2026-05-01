"""``/panic`` Telegram command (FASE 13.6).

Operator escape hatch when something is going badly wrong. Triggers
:func:`mib.trading.panic.execute_panic`: cancels all open orders,
closes all open positions to market with reduceOnly, flips the kill
switch with a 7-day window. Whitelist-gated, group=-1.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_ccxt_trader,
    get_order_repository,
    get_trade_repository,
    get_trading_state_service,
)
from mib.logger import logger
from mib.telegram.formatters import esc
from mib.trading.panic import execute_panic


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


async def panic_cmd(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the panic flow + reply with the report."""
    if update.message is None:
        return
    actor = _actor_for(update)
    logger.warning("/panic invoked by {}", actor)
    try:
        report = await execute_panic(
            actor=actor,
            trader=get_ccxt_trader(),
            order_repo=get_order_repository(),
            trade_repo=get_trade_repository(),
            state_service=get_trading_state_service(),
        )
    except Exception as exc:  # noqa: BLE001 — defensive; execute_panic itself never raises
        logger.error("/panic crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/panic falló inesperadamente:</b> {esc(str(exc))}"
        )
        return

    icon = "🚨" if report.errors else "🛑"
    lines = [
        f"{icon} <b>PANIC ejecutado</b> · actor=<code>{esc(actor)}</code>",
        f"  cancel_count: <code>{report.cancelled_count}</code>",
        f"  close_count: <code>{report.closed_count}</code>",
        f"  elapsed: <code>{report.elapsed_seconds:.2f}s</code>",
    ]
    if report.killed_until is not None:
        lines.append(
            f"  killed_until: <code>{esc(report.killed_until.isoformat())}</code>"
        )
    if report.errors:
        lines.append("\n<b>Errores</b>")
        for e in report.errors[:8]:
            lines.append(f"  • <code>{esc(e[:200])}</code>")
        if len(report.errors) > 8:
            lines.append(f"  • … (+{len(report.errors) - 8} más)")
    else:
        lines.append("\n<i>Sin errores. Kill window 7 días — para reactivar revisa /risk y consulta sesión estratégica.</i>")
    await update.message.reply_html("\n".join(lines))
