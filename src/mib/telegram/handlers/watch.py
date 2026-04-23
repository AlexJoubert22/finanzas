"""/watch + /alerts handlers — price alerts persistence."""

from __future__ import annotations

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from mib.db.models import PriceAlert, User
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.telegram.formatters import (
    esc,
    fmt_alerts_list,
    fmt_watch_created,
)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a price alert. Usage: /watch TICKER op price."""
    if update.message is None or update.effective_user is None:
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_html(
            "Uso: <code>/watch &lt;ticker&gt; &lt;&gt;|&lt;&gt; &lt;precio&gt;</code>\n"
            "Ej. /watch BTC/USDT &gt; 100000"
        )
        return

    ticker = args[0].strip()
    op = args[1].strip()
    try:
        target = float(args[2].replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_html(
            f"Precio inválido: <code>{esc(args[2])}</code>"
        )
        return

    if op not in (">", "<"):
        await update.message.reply_html(
            f"Operador inválido: <code>{esc(op)}</code>. Usa <code>&gt;</code> o <code>&lt;</code>."
        )
        return

    uid = update.effective_user.id
    async with async_session_factory() as session:
        # Ensure the user row exists (the middleware already accepted the uid).
        user = await session.get(User, uid)
        if user is None:
            user = User(telegram_id=uid, username=update.effective_user.username)
            session.add(user)
            await session.flush()
        alert = PriceAlert(
            user_id=uid,
            ticker=ticker,
            operator=op,
            target_price=target,
            is_active=True,
        )
        session.add(alert)
        await session.commit()
        await session.refresh(alert)
        logger.info("/watch: alert #{} created for {} {} {}", alert.id, ticker, op, target)

    await update.message.reply_html(fmt_watch_created(ticker, op, target))


async def alerts(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """List active alerts for the caller with inline delete buttons."""
    if update.message is None or update.effective_user is None:
        return
    uid = update.effective_user.id
    async with async_session_factory() as session:
        stmt = (
            select(PriceAlert)
            .where(PriceAlert.user_id == uid, PriceAlert.is_active.is_(True))
            .order_by(PriceAlert.created_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
    payload = [
        {
            "id": r.id,
            "ticker": r.ticker,
            "operator": r.operator,
            "target_price": r.target_price,
        }
        for r in rows
    ]
    body = fmt_alerts_list(payload)

    if rows:
        buttons = [
            [
                InlineKeyboardButton(
                    f"❌ Borrar #{r.id}", callback_data=f"alert:del:{r.id}"
                )
            ]
            for r in rows[:10]
        ]
        await update.message.reply_html(body, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_html(body)


async def on_alert_delete(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the [❌ Borrar] inline button."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("alert:del:"):
        return
    try:
        alert_id = int(data.split(":")[-1])
    except ValueError:
        return
    uid = update.effective_user.id
    async with async_session_factory() as session:
        alert = await session.get(PriceAlert, alert_id)
        if alert is None or alert.user_id != uid:
            await query.edit_message_text("⚠️ Alerta ya no existe o no es tuya.")
            return
        alert.is_active = False
        await session.commit()
    await query.edit_message_text(f"✅ Alerta #{alert_id} borrada.")
