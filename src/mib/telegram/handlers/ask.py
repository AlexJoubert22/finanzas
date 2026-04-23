"""/ask handler — natural-language question answering.

Mirrors the ``POST /ask`` HTTP pipeline (``plan → fetch → summarise``)
so a Telegram user gets the same quality answer as an API caller.

Spec §6: "/ask desde Telegram es equivalente al /ask del API".
"""

from __future__ import annotations

import asyncio
import contextlib

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_ai_service,
    get_macro_service,
    get_market_service,
    get_news_service,
)
from mib.api.routers.ask import _execute_plan  # noqa: PLC2701 - reuse is intentional
from mib.logger import logger
from mib.telegram.formatters import chunk, fmt_ask_answer

# Same hard ceiling as the HTTP endpoint. Keeps the bot responsive.
_ASK_HARD_TIMEOUT_S = 15.0


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer a natural-language question. Usage: /ask <pregunta>."""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_html(
            "Uso: <code>/ask &lt;pregunta&gt;</code>\n"
            "Ej. <code>/ask cómo está el mercado cripto hoy?</code>"
        )
        return
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_html("Pregunta vacía.")
        return

    # Typing indicator — cosmetic; /ask can take up to 15 s.
    with contextlib.suppress(Exception):
        await update.message.chat.send_chat_action(ChatAction.TYPING)

    try:
        answer = await asyncio.wait_for(
            _run_ask(question),
            timeout=_ASK_HARD_TIMEOUT_S,
        )
    except TimeoutError:
        logger.info("telegram /ask timeout for: {}", question)
        await update.message.reply_html(
            f"⏱ La consulta tardó más de {_ASK_HARD_TIMEOUT_S:.0f}s. "
            "Prueba más tarde o usa los comandos individuales."
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram /ask failed: {}", exc)
        await update.message.reply_html(
            "⚠️ No pude procesar la pregunta. Prueba más tarde."
        )
        return

    body = fmt_ask_answer(question, answer)
    for part in chunk(body):
        await update.message.reply_html(part, disable_web_page_preview=True)


async def _run_ask(question: str) -> str:
    """Execute the same plan → fetch → summarise pipeline as the API."""
    ai = get_ai_service()
    market = get_market_service()
    macro = get_macro_service()
    news = get_news_service()

    plan = await ai.plan_query(question)
    collected = await _execute_plan(plan, market, macro, news)
    try:
        return await ai.summarise_answer(question, plan, collected)
    except Exception as exc:  # noqa: BLE001
        logger.info("telegram /ask summariser soft-fail: {}", exc)
        return (
            "No he podido sintetizar una respuesta. "
            "Prueba con /price, /news o /macro."
        )
