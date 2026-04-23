"""Authentication middleware for the Telegram bot.

Rejects any ``Update`` whose ``effective_user.id`` is not in the
whitelist (spec §6, §13). Reply to the rejected user is a generic
"Acceso no autorizado" — we don't leak the whitelist size or hint at
how to request access.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    BaseHandler,
    ContextTypes,
    TypeHandler,
)

from mib.config import get_settings
from mib.logger import logger
from mib.telegram import BotApp

_REJECT_MESSAGE = "Acceso no autorizado."


class AuthMiddleware:
    """Gate every update at the beginning of the dispatcher chain.

    Implemented as a ``TypeHandler[Update]`` with ``group=-1`` so it
    runs before any command handler. If the user is not whitelisted we
    reply with the generic rejection and ``raise ApplicationHandlerStop``
    to short-circuit the rest of the chain.
    """

    @staticmethod
    async def auth(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return  # should never happen for real messages; silent pass
        allowed = get_settings().telegram_allowed_user_ids
        if user.id in allowed:
            return  # allowed → continue dispatch

        logger.info(
            "telegram: rejected user_id={} username={} command attempt",
            user.id,
            user.username,
        )
        if update.message is not None:
            await update.message.reply_text(_REJECT_MESSAGE)
        # Short-circuit the rest of the dispatcher.
        from telegram.ext import ApplicationHandlerStop

        raise ApplicationHandlerStop

    @classmethod
    def install(cls, app: BotApp) -> None:
        """Register the middleware as the first handler on the app."""
        handler: BaseHandler[Update, ContextTypes.DEFAULT_TYPE, None] = TypeHandler(
            Update, cls.auth
        )
        app.add_handler(handler, group=-1)
