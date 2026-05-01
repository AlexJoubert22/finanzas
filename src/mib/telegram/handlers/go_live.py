"""``/go_live`` + ``/go_live_confirm`` Telegram commands (FASE 14.2).

Operator-only. Whitelist enforced by :class:`AuthMiddleware`.
The two commands wrap :class:`GoLiveFlow.initiate` and
:meth:`GoLiveFlow.confirm` respectively. Errors surface as plain
HTML messages — never silently flip state.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_mode_service,
)
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.operations.go_live import GoLiveFlow
from mib.telegram.formatters import esc


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


def _flow() -> GoLiveFlow:
    return GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=get_mode_service(),
    )


async def go_live_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    args = context.args or []
    reason = " ".join(args).strip()
    actor = _actor_for(update)
    if not reason:
        await update.message.reply_html(
            "Uso: <code>/go_live &lt;reason &ge;30 chars&gt;</code>\n"
            "Ejemplo: <code>/go_live PAPER validation 35d 52 trades all "
            "checks green ready for live capital</code>"
        )
        return
    try:
        result = await _flow().initiate(actor=actor, reason=reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("/go_live crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/go_live falló inesperadamente:</b> {esc(str(exc))}"
        )
        return

    if result.accepted:
        await update.message.reply_html(
            "📧 <b>Código 2FA enviado</b>\n"
            f"  pending_id: <code>{esc(result.pending_id or '')}</code>\n"
            f"  expira en: <code>{result.ttl_seconds}s</code>\n"
            "Revisa tu email del operador y confirma con:\n"
            "<code>/go_live_confirm &lt;código&gt;</code>\n"
            "<i>Mínimo 30s antes de confirmar.</i>"
        )
        return

    # Rejected.
    body = f"🚫 <b>/go_live rechazado:</b> <code>{esc(result.reason or '')}</code>"
    if result.preflight is not None and not result.preflight.ready:
        body += "\n\n<b>Preflight no listo</b>:"
        for c in result.preflight.failed_critical[:5]:
            body += f"\n  ❌ {esc(c.name)}: <code>{esc(c.details[:200])}</code>"
    await update.message.reply_html(body)


async def go_live_confirm_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Uso: <code>/go_live_confirm &lt;código&gt;</code>"
        )
        return
    code = args[0].strip()
    actor = _actor_for(update)
    try:
        result = await _flow().confirm(actor=actor, code=code)
    except Exception as exc:  # noqa: BLE001
        logger.error("/go_live_confirm crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/go_live_confirm falló inesperadamente:</b> {esc(str(exc))}"
        )
        return

    if result.accepted:
        logger.warning(
            "go_live: ACTIVATED LIVE actor={} transition_id={}",
            actor, result.transition_id,
        )
        await update.message.reply_html(
            "🟢 <b>LIVE ACTIVATED</b>\n"
            f"  actor: <code>{esc(actor)}</code>\n"
            f"  mode_transition: <code>#{result.transition_id}</code>\n"
            "<i>trading_state.enabled=True, mode=LIVE.\n"
            "Sizing reducido al 50% durante los primeros 30 días "
            "(FASE 14.3).</i>"
        )
        return

    await update.message.reply_html(
        f"🚫 <b>/go_live_confirm rechazado:</b> "
        f"<code>{esc(result.reason or 'unknown')}</code>"
    )
