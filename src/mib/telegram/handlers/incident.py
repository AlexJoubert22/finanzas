"""``/incident`` Telegram command (FASE 13.4).

Operator-only manual incident registration. Default type is
``MANUAL_INTERVENTION_REQUIRED`` (the catch-all for "I need to flag
something for the audit log without firing a kill switch"). The
operator can override the type by passing one of the 7 enum values
as the first argument.

Whitelist enforced by :class:`AuthMiddleware`; registered at
group=-1 so it bypasses any blockage in the default chain.

Usage:
- ``/incident <reason>``                     → MANUAL_INTERVENTION_REQUIRED
- ``/incident <type> <reason>``              → explicit type if known
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_incident_emitter
from mib.logger import logger
from mib.observability.incidents import CriticalIncidentType
from mib.telegram.formatters import esc

_VALID_TYPES: tuple[str, ...] = tuple(t.value for t in CriticalIncidentType)


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


async def incident_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return

    args = context.args or []
    if not args:
        await update.message.reply_html(
            "Uso:\n"
            "<code>/incident &lt;reason&gt;</code>\n"
            "<code>/incident &lt;type&gt; &lt;reason&gt;</code>\n\n"
            "Tipos válidos:\n"
            + "\n".join(f"  • <code>{esc(t)}</code>" for t in _VALID_TYPES)
        )
        return

    # Parse: first arg may be a type or part of the reason.
    first = args[0].strip()
    if first in _VALID_TYPES:
        type_ = CriticalIncidentType(first)
        reason = " ".join(args[1:]).strip()
    else:
        type_ = CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED
        reason = " ".join(args).strip()

    if not reason:
        await update.message.reply_html(
            "⚠️ Falta una <i>razón</i>. La razón es obligatoria para "
            "que la entrada de auditoría sea útil."
        )
        return

    actor = _actor_for(update)
    severity = (
        "critical"
        if type_ != CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED
        else "warning"
    )

    emitter = get_incident_emitter()
    try:
        new_id = await emitter.emit(
            type_=type_,
            context={
                "actor": actor,
                "reason": reason[:1000],
                "source": "telegram_manual",
            },
            severity=severity,  # type: ignore[arg-type]
            auto_detected=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/incident emit crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/incident fallo:</b> {esc(str(exc))}"
        )
        return

    await update.message.reply_html(
        "📝 <b>Incidente registrado</b>\n"
        f"  id: <code>#{new_id}</code>\n"
        f"  type: <code>{esc(type_.value)}</code>\n"
        f"  severity: <code>{esc(severity)}</code>\n"
        f"  actor: <code>{esc(actor)}</code>\n"
        f"  reason: <code>{esc(reason[:400])}</code>"
    )
