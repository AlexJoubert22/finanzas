"""``/preflight`` Telegram command (FASE 14.1)."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.logger import logger
from mib.operations.preflight import format_preflight_html, run_preflight
from mib.telegram.formatters import esc


async def preflight_cmd(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    try:
        report = await run_preflight()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.error("/preflight crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/preflight falló inesperadamente:</b> {esc(str(exc))}"
        )
        return
    await update.message.reply_html(format_preflight_html(report))
