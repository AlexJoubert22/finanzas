"""/macro handler — SPX, VIX, DXY, 10Y yield, BTC dominance."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_macro_service
from mib.logger import logger
from mib.telegram.formatters import fmt_macro_card


async def macro(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    try:
        resp = await get_macro_service().snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("/macro failed: {}", exc)
        await update.message.reply_html("⚠️ No pude obtener el snapshot macro.")
        return
    body = fmt_macro_card(resp.model_dump())
    await update.message.reply_html(body)
