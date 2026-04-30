"""``/reconcile`` Telegram command — operator-triggered reconciliation pass.

Whitelist-gated by :class:`AuthMiddleware`. Runs in the same priority
group as ``/risk`` since the operator likely cycles between the two
when triaging an incident.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_reconciler
from mib.logger import logger
from mib.telegram.formatters import esc


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


async def reconcile_cmd(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Trigger one reconciliation pass and post a summary."""
    if update.message is None:
        return
    actor = _actor_for(update)
    reconciler = get_reconciler()
    try:
        report = await reconciler.reconcile(triggered_by=f"telegram:{actor}")
    except Exception as exc:  # noqa: BLE001 — defensive surface
        logger.error("/reconcile failed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/reconcile falló:</b> {esc(str(exc))}"
        )
        return

    if report.status == "error":
        await update.message.reply_html(
            "❌ <b>Reconciler error</b>\n"
            f"{esc(report.error_message or 'unknown')}"
        )
        return

    icon = "✅" if report.status == "ok" else "⚠️"
    lines = [
        f"{icon} <b>Reconcile #{report.run_id}</b> · status=<code>{report.status}</code>",
        f"  orphan_exchange: <code>{report.orphan_exchange_count}</code>",
        f"  orphan_db: <code>{report.orphan_db_count}</code>",
        f"  balance_drift: <code>{report.balance_drift_count}</code>",
    ]
    for d in report.discrepancies[:5]:
        lines.append(f"• [{d.kind}] {esc(d.summary)}")
    if len(report.discrepancies) > 5:
        lines.append(f"• … (+{len(report.discrepancies) - 5} more)")
    await update.message.reply_html("\n".join(lines))
