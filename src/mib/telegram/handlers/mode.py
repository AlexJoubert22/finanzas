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

from mib.api.dependencies import (
    get_alerter,
    get_mode_service,
    get_trading_state_service,
)
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.telegram.formatters import esc
from mib.trading.mode import TradingMode
from mib.trading.mode_service import (
    MIN_FORCE_REASON_LEN,
    ForceRateLimitExceededError,
    ForceReasonTooShortError,
)
from mib.trading.mode_status import build_mode_status, format_mode_status_html
from mib.trading.mode_transitions_repo import ModeTransitionRepository

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


async def mode_status_cmd(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/mode_status`` — current mode + projection of the next allowed mode.

    Reads ``mode_transitions`` for the audit context and computes the
    gate progress (days_in_mode / closed_trades / clean_streak) so
    the operator sees exactly what's left before climbing the ladder.
    """
    if update.message is None:
        return
    service = get_mode_service()
    repo = ModeTransitionRepository(async_session_factory)
    try:
        current = await service.get_current()
        status = await build_mode_status(
            current=current,
            transitions_repo=repo,
            session_factory=async_session_factory,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/mode_status failed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/mode_status falló:</b> {esc(str(exc))}"
        )
        return
    await update.message.reply_html(format_mode_status_html(status))


async def mode_force_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/mode_force <name> <reason>`` — bypass guards with reinforced audit.

    Triple audit:
    1. ``mode_transitions`` row with ``override_used=True``.
    2. Telegram alert to admin (even if the actor IS the admin) with
       a visual emphasis on FORCE.
    3. structlog WARNING with full context.

    Constraints (enforced in :meth:`ModeService.force_transition_to`):
    - ``reason`` >= 20 chars
    - Max 1 force per actor per 7-day rolling window
    """
    if update.message is None:
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_html(
            "Uso: <code>/mode_force &lt;off|shadow|paper|semi_auto|live&gt; "
            "&lt;reason &ge;20 chars&gt;</code>"
        )
        return

    raw = args[0].lower().strip()
    if raw not in _VALID_NAMES:
        await update.message.reply_html(
            f"⚠️ Modo inválido: <code>{esc(raw)}</code>.\n"
            f"Válidos: <code>{', '.join(_VALID_NAMES)}</code>"
        )
        return
    target = TradingMode(raw)
    reason = " ".join(args[1:]).strip()
    actor = _actor_for(update)
    service = get_mode_service()

    try:
        result = await service.force_transition_to(
            target, actor=actor, reason=reason
        )
    except ForceReasonTooShortError as exc:
        await update.message.reply_html(
            f"⚠️ <b>Reason demasiado corto</b> "
            f"({exc.length} chars, mínimo {MIN_FORCE_REASON_LEN}). "
            "Justifica el override claramente."
        )
        return
    except ForceRateLimitExceededError as exc:
        await update.message.reply_html(
            "🚫 <b>force_rate_limit_exceeded</b>\n"
            f"  actor <code>{esc(actor)}</code> ya usó "
            f"{exc.window_count} force(s) en los últimos 7 días "
            f"(límite {exc.limit}). Espera o pide a otro operador."
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("/mode_force crashed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/mode_force falló:</b> {esc(str(exc))}"
        )
        return

    if not result.allowed:
        # The only no-force-friendly rejection is no_op_transition.
        await update.message.reply_html(
            "🚫 <b>Transición rechazada</b>\n"
            f"  motivo: <code>{esc(result.reason or 'unknown')}</code>"
        )
        return

    # Triple audit: structlog WARNING + Telegram alert + DB row already
    # written via override_used=True in transition_to.
    logger.warning(
        "mode_force: {} -> {} actor={} reason={!r} transition_id={}",
        result.from_mode,
        result.to_mode,
        actor,
        reason,
        result.transition_id,
    )
    alerter = get_alerter()
    try:
        await alerter.alert(
            "⚠️ <b>MODE FORCE ejecutado</b>\n"
            f"  <code>{esc(result.from_mode.value)}</code> → "
            f"<code>{esc(result.to_mode.value)}</code>\n"
            f"  actor: <code>{esc(actor)}</code>\n"
            f"  reason: <code>{esc(reason)}</code>\n"
            f"  transition_id: <code>#{result.transition_id}</code>"
        )
    except Exception as alert_exc:  # noqa: BLE001 — best-effort
        logger.warning("mode_force: alerter failed: {}", alert_exc)

    await update.message.reply_html(
        "⚠️ <b>FORCE aplicado</b>\n"
        f"  <code>{esc(result.from_mode.value)}</code> → "
        f"<code>{esc(result.to_mode.value)}</code>\n"
        f"  reason: <code>{esc(reason)}</code>\n"
        f"  transition_id: <code>#{result.transition_id}</code>\n"
        "<i>Audit: mode_transitions.override_used=True + admin alert + log.</i>"
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
