"""``/wind_down`` + ``/shutdown`` Telegram commands (FASE 14.5).

Operator-only (whitelist enforced by :class:`AuthMiddleware`). Both
delegate to :class:`WindDownService.start` — they only differ in the
``kind`` recorded for audit:

- ``wind_down``: planned graceful exit ("we're pausing").
- ``shutdown``: terminal intent ("we're done with this run").

Neither force-closes positions; existing open trades keep their
native stops/targets and exit naturally. Use ``/panic`` (FASE 13.6)
when force-flat is required.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_trade_repository,
    get_trading_state_service,
)
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.operations.wind_down import WindDownService
from mib.telegram.formatters import esc


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


def _service() -> WindDownService:
    return WindDownService(
        session_factory=async_session_factory,
        state_service=get_trading_state_service(),
        trade_repo=get_trade_repository(),
    )


async def _handle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    kind: str,
    label: str,
) -> None:
    if update.message is None:
        return
    args = context.args or []
    reason = " ".join(args).strip()
    actor = _actor_for(update)
    if not reason:
        await update.message.reply_html(
            f"Uso: <code>/{kind} &lt;reason &ge;20 chars&gt;</code>"
        )
        return
    try:
        result = await _service().start(
            actor=actor,
            reason=reason,
            kind=kind,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/{} crashed: {}", kind, exc)
        await update.message.reply_html(
            f"❌ <b>/{esc(kind)} falló inesperadamente:</b> "
            f"{esc(str(exc))}"
        )
        return

    if not result.accepted:
        await update.message.reply_html(
            f"🚫 <b>/{esc(kind)} rechazado:</b> "
            f"<code>{esc(result.reason or 'unknown')}</code>"
        )
        return

    if result.positions_at_start == 0:
        body = (
            f"🟡 <b>{label} iniciado y completado al instante</b>\n"
            f"  wind_down_id: <code>{result.wind_down_id}</code>\n"
            f"  posiciones al inicio: <code>0</code>\n"
            "<i>trading_state.enabled=False. Sin posiciones que esperar.</i>"
        )
    else:
        body = (
            f"🟡 <b>{label} iniciado</b>\n"
            f"  wind_down_id: <code>{result.wind_down_id}</code>\n"
            f"  posiciones al inicio: "
            f"<code>{result.positions_at_start}</code>\n"
            "<i>trading_state.enabled=False (no nuevas entradas).\n"
            "Las posiciones abiertas conservan stops nativos y se "
            "cerrarán al disparar SL/TP.</i>\n"
            "Estado: <code>/risk</code>."
        )
    await update.message.reply_html(body)


async def wind_down_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle(update, context, kind="wind_down", label="Wind-down")


async def shutdown_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle(update, context, kind="shutdown", label="Shutdown")
