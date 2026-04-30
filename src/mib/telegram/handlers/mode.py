"""``/mode`` Telegram command (FASE 10.1).

- ``/mode`` (no args)         → shows current mode + last modified.
- ``/mode <name>``            → attempts a transition through guards.
- ``/mode_status``            → projection of next-allowed mode (10.4).
- ``/mode_force <name> <r>``  → bypasses guards with audit reinforcing
  rules (10.5).

Whitelist-gated by :class:`AuthMiddleware`. The transition handlers
register at ``group=-1`` so an operator can flip mode even if the
default group=0 chain is blocked.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_mode_service, get_trading_state_service
from mib.logger import logger
from mib.telegram.formatters import esc
from mib.trading.mode import TradingMode

_VALID_NAMES: tuple[str, ...] = tuple(m.value for m in TradingMode)


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/mode``                 — show current mode + audit context.
    ``/mode <name>``              — transition to a TradingMode.
    """
    if update.message is None:
        return

    args = context.args or []
    if not args:
        await _show_current(update)
        return

    raw = args[0].lower().strip()
    if raw not in _VALID_NAMES:
        await update.message.reply_html(
            f"⚠️ Modo inválido: <code>{esc(raw)}</code>.\n"
            f"Válidos: <code>{', '.join(_VALID_NAMES)}</code>"
        )
        return

    target = TradingMode(raw)
    reason = " ".join(args[1:]).strip() or None
    actor = _actor_for(update)
    service = get_mode_service()
    try:
        result = await service.transition_to(
            target, actor=actor, reason=reason
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/mode transition crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/mode falló:</b> {esc(str(exc))}"
        )
        return

    if not result.allowed:
        await update.message.reply_html(
            "🚫 <b>Transición rechazada</b>\n"
            f"  desde: <code>{esc(result.from_mode.value)}</code>\n"
            f"  a:     <code>{esc(result.to_mode.value)}</code>\n"
            f"  motivo: <code>{esc(result.reason or 'unknown')}</code>"
        )
        return

    await update.message.reply_html(
        "✅ <b>Modo transicionado</b>\n"
        f"  <code>{esc(result.from_mode.value)}</code> → "
        f"<code>{esc(result.to_mode.value)}</code>\n"
        f"  actor: <code>{esc(actor)}</code>\n"
        + (f"  reason: <code>{esc(reason)}</code>\n" if reason else "")
        + (
            f"  transition_id: <code>#{result.transition_id}</code>"
            if result.transition_id is not None
            else "<i>(audit log activates en FASE 10.2)</i>"
        )
    )


async def _show_current(update: Update) -> None:
    if update.message is None:
        return
    service = get_mode_service()
    state_service = get_trading_state_service()
    current = await service.get_current()
    state = await state_service.get()
    await update.message.reply_html(
        "📊 <b>Modo actual</b>\n"
        f"  <code>{esc(current.value)}</code>\n"
        f"  trading_enabled: <code>{state.enabled}</code>\n"
        f"  last_modified_by: <code>{esc(state.last_modified_by)}</code>\n"
        f"  last_modified_at: <code>{esc(str(state.last_modified_at))}</code>\n"
        "<i>Usa <code>/mode &lt;off|shadow|paper|semi_auto|live&gt; "
        "[reason]</code> para transicionar.</i>"
    )
