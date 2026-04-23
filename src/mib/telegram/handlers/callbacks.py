"""Callback handlers for inline-keyboard buttons.

Current button namespaces:
    - ``price:refresh:<TICKER>``    — re-render the /price card
    - ``price:chart4h:<TICKER>``    — ship a 4h candlestick chart
    - ``price:watch:<TICKER>``      — prompt /watch usage

``alert:del:<ID>`` lives in ``handlers.watch`` because it manipulates
the same domain models.
"""

from __future__ import annotations

import contextlib
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_service, get_market_service
from mib.indicators.charting import candles_dataframe, render_candles_png
from mib.logger import logger
from mib.telegram.formatters import esc, fmt_price_card


async def on_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch ``price:*`` callback data."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "price":
        return
    action, ticker = parts[1], parts[2]

    if action == "refresh":
        await _refresh(update, ticker)
    elif action == "chart4h":
        await _chart4h(update, context, ticker)
    elif action == "watch":
        await _watch_hint(update, ticker)


async def _refresh(update: Update, ticker: str) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        market = get_market_service()
        resp = await market.get_symbol(ticker, ohlcv_timeframe="1h", ohlcv_limit=250)
        ai_analysis = await get_ai_service().symbol_analysis(resp)
        if ai_analysis:
            resp = resp.model_copy(update={"ai_analysis": ai_analysis})
    except Exception as exc:  # noqa: BLE001
        logger.warning("price:refresh {} failed: {}", ticker, exc)
        await query.edit_message_text(
            f"⚠️ No pude refrescar <code>{esc(ticker)}</code>.",
            parse_mode="HTML",
        )
        return

    buttons = [
        [
            InlineKeyboardButton("🔄 Refrescar", callback_data=f"price:refresh:{ticker}"),
            InlineKeyboardButton("📊 Chart 4h", callback_data=f"price:chart4h:{ticker}"),
            InlineKeyboardButton("👁 Watch", callback_data=f"price:watch:{ticker}"),
        ]
    ]
    await query.edit_message_text(
        fmt_price_card(resp.model_dump()),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


async def _chart4h(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ticker: str,
) -> None:
    query = update.callback_query
    if query is None:
        return
    # query.message is typed as ``MaybeInaccessibleMessage`` — narrow to
    # the concrete ``Message`` (has ``reply_html`` / ``reply_photo``) or
    # fall back to context.bot with the chat id.
    msg = query.message if isinstance(query.message, Message) else None
    chat_id = msg.chat_id if msg else (query.from_user.id if query.from_user else None)
    if chat_id is None:
        return

    try:
        market = get_market_service()
        data = await market.get_symbol(ticker, ohlcv_timeframe="4h", ohlcv_limit=120)
    except Exception as exc:  # noqa: BLE001
        logger.warning("price:chart4h {} failed: {}", ticker, exc)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ No pude obtener datos de <code>{esc(ticker)}</code>.",
            parse_mode="HTML",
        )
        return

    df = candles_dataframe([c.model_dump() for c in data.candles])
    if df.empty:
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Sin datos para graficar.")
        return
    path = await render_candles_png(df, title=ticker, timeframe="4h")
    if path is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Chart temporalmente no disponible.",
        )
        return
    try:
        with open(path, "rb") as f:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"📊 <b>{esc(ticker)}</b> · 4h",
                parse_mode="HTML",
            )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


async def _watch_hint(update: Update, ticker: str) -> None:
    query = update.callback_query
    if query is None or not isinstance(query.message, Message):
        return
    await query.message.reply_html(
        f"Para crear una alerta en <b>{esc(ticker)}</b>:\n"
        f"<code>/watch {esc(ticker)} &gt; PRECIO</code>  (alerta al subir)\n"
        f"<code>/watch {esc(ticker)} &lt; PRECIO</code>  (alerta al bajar)"
    )
