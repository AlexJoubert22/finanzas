"""/price handler — precio + indicadores + análisis IA."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_service, get_market_service
from mib.logger import logger
from mib.telegram.formatters import fmt_price_card


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_html(
            "Uso: <code>/price &lt;ticker&gt;</code>\nEj. /price BTC/USDT"
        )
        return

    ticker = context.args[0].strip()
    try:
        market = get_market_service()
        resp = await market.get_symbol(ticker, ohlcv_timeframe="1h", ohlcv_limit=250)
        ai_analysis = await get_ai_service().symbol_analysis(resp)
        if ai_analysis:
            resp = resp.model_copy(update={"ai_analysis": ai_analysis})
    except Exception as exc:  # noqa: BLE001
        logger.warning("/price {} failed: {}", ticker, exc)
        await update.message.reply_html(
            f"⚠️ No pude obtener datos de <code>{ticker}</code>. Prueba más tarde."
        )
        return

    body = fmt_price_card(resp.model_dump())
    buttons = [
        [
            InlineKeyboardButton("🔄 Refrescar", callback_data=f"price:refresh:{ticker}"),
            InlineKeyboardButton("📊 Chart 4h", callback_data=f"price:chart4h:{ticker}"),
            InlineKeyboardButton("👁 Watch", callback_data=f"price:watch:{ticker}"),
        ]
    ]
    await update.message.reply_html(
        body,
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )
