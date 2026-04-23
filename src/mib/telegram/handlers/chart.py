"""/chart handler — render candlestick PNG via ``indicators.charting``."""

from __future__ import annotations

import contextlib
import os

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_market_service
from mib.indicators.charting import candles_dataframe, render_candles_png
from mib.logger import logger

_ALLOWED_TF = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1wk"}


async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_html(
            "Uso: <code>/chart &lt;ticker&gt; [tf]</code>\nEj. /chart BTC/USDT 4h"
        )
        return

    ticker = context.args[0].strip()
    tf = (context.args[1].strip().lower() if len(context.args) > 1 else "1h")
    if tf not in _ALLOWED_TF:
        await update.message.reply_html(
            f"Timeframe desconocido: <code>{tf}</code>. "
            f"Usa uno de: {', '.join(sorted(_ALLOWED_TF))}."
        )
        return

    try:
        market = get_market_service()
        data = await market.get_symbol(ticker, ohlcv_timeframe=tf, ohlcv_limit=120)
    except Exception as exc:  # noqa: BLE001
        logger.warning("/chart {} {} failed: {}", ticker, tf, exc)
        await update.message.reply_html(
            f"⚠️ No pude obtener datos de <code>{ticker}</code>."
        )
        return

    candles = [c.model_dump() for c in data.candles]
    df = candles_dataframe(candles)
    if df.empty:
        await update.message.reply_html("⚠️ No hay velas suficientes para graficar.")
        return

    path = await render_candles_png(df, title=ticker, timeframe=tf)
    if path is None:
        await update.message.reply_html(
            "⚠️ Chart temporalmente no disponible (demasiadas peticiones en curso)."
        )
        return

    try:
        with open(path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"📊 <b>{ticker}</b> · {tf}",
                parse_mode="HTML",
            )
    finally:
        # Mitigation 2: never keep the PNG in Python RAM, unlink afterwards.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
