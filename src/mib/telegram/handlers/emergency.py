"""Emergency commands: ``/stop``, ``/freeze``, ``/risk``.

Registered at high priority (``group=-1``) so they run before the
default command handler chain. The intent is that even under load —
or even if a downstream gate is misbehaving — the operator's kill
switch always works.

Whitelist enforcement is still inherited from :class:`AuthMiddleware`
because middleware also runs in ``group=-1`` BEFORE these handlers.
A non-whitelisted user attempting ``/stop`` is short-circuited at
the middleware boundary, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_risk_decision_repository,
    get_signal_repository,
    get_trading_state_service,
)
from mib.logger import logger
from mib.telegram.formatters import esc, fmt_price


def _actor_for(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


# ─── /stop ────────────────────────────────────────────────────────────

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Master kill switch ON. Existing positions stay open; no new
    signals leave the gates.

    Optional argument: a reason string captured in
    ``trading_state.last_modified_by`` for audit.
    """
    if update.message is None:
        return
    actor = _actor_for(update)
    reason = " ".join(context.args or []).strip() or "no reason given"
    state_service = get_trading_state_service()
    try:
        new_state = await state_service.update(
            actor=f"{actor} reason={reason!r}", enabled=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/stop failed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/stop falló:</b> {esc(str(exc))}"
        )
        return
    logger.warning("/stop invoked by {} reason={!r}", actor, reason)
    await update.message.reply_html(
        "🛑 <b>Kill switch ON</b>\n"
        f"trading_state.enabled = {new_state.enabled}\n"
        f"Modificado por: <code>{esc(new_state.last_modified_by)}</code>\n"
        "Las posiciones abiertas mantienen sus stops nativos.\n"
        "Para reactivar: <code>/risk</code> y consultar con sesión estratégica."
    )


# ─── /freeze ──────────────────────────────────────────────────────────

async def freeze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same effect as ``/stop`` (no new signals). Semantic distinction
    in the audit log: ``/freeze`` documents that the operator
    deliberately holds existing positions open while pausing new ones.
    """
    if update.message is None:
        return
    actor = _actor_for(update)
    reason = " ".join(context.args or []).strip() or "freeze: hold existing"
    state_service = get_trading_state_service()
    try:
        new_state = await state_service.update(
            actor=f"{actor} reason={reason!r}", enabled=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("/freeze failed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/freeze falló:</b> {esc(str(exc))}"
        )
        return
    logger.warning("/freeze invoked by {} reason={!r}", actor, reason)
    await update.message.reply_html(
        "🧊 <b>Freeze</b>\n"
        f"trading_state.enabled = {new_state.enabled}\n"
        f"Actor: <code>{esc(new_state.last_modified_by)}</code>\n"
        "<i>Posiciones abiertas se mantienen con sus stops nativos.</i>\n"
        "Sin nuevas signals hasta reactivación."
    )


# ─── /risk ────────────────────────────────────────────────────────────

async def risk_cmd(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snapshot of current risk state: trading_state, last
    RiskDecision per recent pending signal, days_clean_streak hint.
    """
    if update.message is None:
        return

    state_service = get_trading_state_service()
    decision_repo = get_risk_decision_repository()
    signal_repo = get_signal_repository()

    try:
        state = await state_service.get()
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_html(
            f"❌ trading_state no disponible: {esc(str(exc))}"
        )
        return

    pending = await signal_repo.list_pending(limit=5)

    lines = [
        "🛡 <b>Estado de risk</b>",
        "",
        "<b>trading_state</b>",
        f"  enabled: <code>{state.enabled}</code>",
        f"  daily_dd_max_pct: <code>{fmt_price(state.daily_dd_max_pct, decimals=4)}</code>",
        f"  total_dd_max_pct: <code>{fmt_price(state.total_dd_max_pct, decimals=4)}</code>",
        f"  killed_until: <code>{esc(state.killed_until or 'none')}</code>",
        f"  last_modified_by: <code>{esc(state.last_modified_by)}</code>",
        f"  last_modified_at: <code>{esc(state.last_modified_at)}</code>",
        "",
        f"<b>Signals pending recientes:</b> {len(pending)}",
    ]

    for ps in pending[:3]:
        latest = await decision_repo.latest_for_signal(ps.id)
        if latest is None:
            decision_summary = "<i>sin decisión aún</i>"
        else:
            verdict = "✅ approved" if latest.approved else "🚫 rejected"
            sized = (
                f"sized={latest.sized_amount}"
                if latest.sized_amount is not None
                else "no size"
            )
            decision_summary = f"v{latest.version} {verdict} ({sized})"
        lines.append(
            f"  #{ps.id} <code>{esc(ps.signal.ticker)}</code> · "
            f"{esc(ps.signal.strategy_id)} · {decision_summary}"
        )

    age_seconds = (
        datetime.now(UTC).replace(tzinfo=None)
        - (
            state.last_modified_at.replace(tzinfo=None)
            if state.last_modified_at.tzinfo is not None
            else state.last_modified_at
        )
    ).total_seconds()
    lines.append("")
    lines.append(
        f"<i>Último cambio en trading_state hace {int(age_seconds)}s.</i>"
    )

    await update.message.reply_html("\n".join(lines))
