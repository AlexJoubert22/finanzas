"""Telegram bot setup: build the ``Application``, wire handlers, run polling.

Integration pattern:

    - ``build_application()`` — pure factory. Returns a ready-to-start
      ``telegram.ext.Application`` with all handlers + middleware bound.
    - ``start_bot() / stop_bot()`` — async lifecycle used from the
      FastAPI lifespan. Polling runs inside the same asyncio loop as
      uvicorn; PTB's ``Application`` manages its own internal tasks.
    - ``register_bot_jobs()`` — called by the scheduler module once the
      bot is up so the 3 background jobs can call ``app.bot.send_message``.

We purposely do NOT expose the Application to the FastAPI layer —
everything the HTTP handlers need lives in ``api.dependencies`` already.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from mib.config import get_settings
from mib.logger import logger
from mib.telegram import BotApp
from mib.telegram.handlers.ask import ask as ask_handler
from mib.telegram.handlers.backtest import backtest_cmd
from mib.telegram.handlers.callbacks import on_price_callback
from mib.telegram.handlers.chart import chart as chart_handler
from mib.telegram.handlers.emergency import (
    freeze_cmd,
    risk_cmd,
    stop_cmd,
)
from mib.telegram.handlers.macro import macro as macro_handler
from mib.telegram.handlers.mode import mode_cmd, mode_force_cmd, mode_status_cmd
from mib.telegram.handlers.news import news as news_handler
from mib.telegram.handlers.price import price as price_handler
from mib.telegram.handlers.reconcile import reconcile_cmd
from mib.telegram.handlers.scan import scan as scan_handler
from mib.telegram.handlers.signals import (
    on_signal_callback,
    signals_cmd,
)
from mib.telegram.handlers.start import help_cmd
from mib.telegram.handlers.start import start as start_handler
from mib.telegram.handlers.status import status as status_handler
from mib.telegram.handlers.watch import (
    alerts as alerts_handler,
)
from mib.telegram.handlers.watch import (
    on_alert_delete,
)
from mib.telegram.handlers.watch import (
    watch as watch_handler,
)
from mib.telegram.middleware import AuthMiddleware

_app: BotApp | None = None


def build_application() -> BotApp:
    """Construct the ``telegram.ext.Application`` with all handlers wired."""
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is empty — bot cannot start. "
            "Set it in .env or leave MIB in API-only mode."
        )

    # We don't install PTB's optional ``[rate-limiter]`` extra — aiolimiter
    # is a full transitive and we'd rather stay lean. PTB still honours the
    # Telegram API's own rate-limit response codes; we just don't pace
    # outbound calls ourselves. Acceptable because our bot is low-volume
    # (single-digit users, background jobs ≤ 1 msg/minute).
    app = ApplicationBuilder().token(token).build()

    # Middleware first — group=-1 runs before command handlers.
    AuthMiddleware.install(app)

    # Emergency commands at group=-1 so they bypass any blockage in the
    # default group=0 chain. The middleware whitelist still applies
    # because it also lives in group=-1 and runs first.
    app.add_handler(CommandHandler("stop", stop_cmd), group=-1)
    app.add_handler(CommandHandler("freeze", freeze_cmd), group=-1)
    app.add_handler(CommandHandler("risk", risk_cmd), group=-1)
    app.add_handler(CommandHandler("reconcile", reconcile_cmd), group=-1)
    app.add_handler(CommandHandler("backtest", backtest_cmd), group=-1)
    app.add_handler(CommandHandler("mode", mode_cmd), group=-1)
    app.add_handler(CommandHandler("mode_status", mode_status_cmd), group=-1)
    app.add_handler(CommandHandler("mode_force", mode_force_cmd), group=-1)

    # Commands
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_handler))
    app.add_handler(CommandHandler("chart", chart_handler))
    app.add_handler(CommandHandler("scan", scan_handler))
    app.add_handler(CommandHandler("news", news_handler))
    app.add_handler(CommandHandler("macro", macro_handler))
    app.add_handler(CommandHandler("watch", watch_handler))
    app.add_handler(CommandHandler("alerts", alerts_handler))
    app.add_handler(CommandHandler("ask", ask_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("signals", signals_cmd))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(on_price_callback, pattern=r"^price:"))
    app.add_handler(CallbackQueryHandler(on_alert_delete, pattern=r"^alert:del:"))
    app.add_handler(CallbackQueryHandler(on_signal_callback, pattern=r"^sig:"))

    return app


async def start_bot() -> BotApp | None:
    """Initialise + start polling. Returns None if the bot is disabled."""
    global _app  # noqa: PLW0603
    if _app is not None:
        return _app
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.info("telegram: TELEGRAM_BOT_TOKEN empty — bot disabled (API-only mode)")
        return None

    app = build_application()
    await app.initialize()
    await app.start()
    if app.updater is None:  # updater is None only when JobQueue is disabled
        raise RuntimeError("telegram: Application has no Updater configured")
    # allowed_updates=None → PTB defaults (message + callback_query etc.)
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    _app = app
    logger.info("telegram: bot started (polling)")
    return app


async def stop_bot() -> None:
    """Stop polling + shutdown — idempotent."""
    global _app  # noqa: PLW0603
    if _app is None:
        return
    try:
        if _app.updater and _app.updater.running:
            await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: shutdown error: {}", exc)
    finally:
        _app = None
        logger.info("telegram: bot stopped")


def get_bot_app() -> BotApp | None:
    """Access the running ``Application`` (e.g. for scheduler jobs)."""
    return _app
