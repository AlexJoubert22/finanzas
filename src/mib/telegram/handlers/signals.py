"""/signals command + inline-keyboard callbacks for Signal approval.

Subcommands of ``/signals``:

- ``/signals pending``                       — list every pending signal.
- ``/signals run <preset> <tickers_csv>``    — ad-hoc scan + persist +
  Telegram notify (deliberately invoked manually for now; a scheduler
  binding can be wired in a follow-up).

Inline buttons attached to each emitted signal card:

- ``sig:ok:<id>``     → mark as ``consumed``  (operator approved)
- ``sig:no:<id>``     → mark as ``cancelled`` (operator discarded)
- ``sig:chart:<id>``  → ship a 4h chart of the signal's ticker

No order is placed yet — execution is FASE 9. Approval here only
flips the DB status so the operator knows the signal was acted on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from telegram import InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from mib.api.dependencies import (
    get_portfolio_state,
    get_risk_decision_repository,
    get_risk_manager,
    get_signal_repository,
)
from mib.indicators.charting import candles_dataframe, render_candles_png
from mib.logger import logger
from mib.services.market import MarketService
from mib.telegram.formatters import (
    esc,
    fmt_pending_signals_list,
    fmt_signal_card,
)
from mib.trading.notify import scanner_to_signals_job
from mib.trading.risk.decision import RiskDecision
from mib.trading.signal_repo import StaleSignalStateError

#: Threshold beyond which an approval re-evaluates risk before
#: confirming. Keeps the operator's "✅" honest if the click lags the
#: decision (e.g. they walked away from the chat for a while).
_DECISION_STALE_AFTER = timedelta(minutes=5)

_VALID_PRESETS = {"oversold", "breakout", "trending"}


# ─── /signals command ──────────────────────────────────────────────

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    args = context.args or []
    if not args or args[0].lower() == "pending":
        await _show_pending(update)
        return

    sub = args[0].lower()
    if sub == "run":
        await _run_scan(update, context, args[1:])
        return

    await update.message.reply_html(
        "Uso:\n"
        "<code>/signals pending</code>\n"
        "<code>/signals run &lt;preset&gt; &lt;BTC/USDT,ETH/USDT&gt;</code>"
    )


async def _show_pending(update: Update) -> None:
    if update.message is None:
        return
    repo = get_signal_repository()
    pending = await repo.list_pending()
    await update.message.reply_html(
        fmt_pending_signals_list(pending), disable_web_page_preview=True
    )


async def _run_scan(
    update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    if update.message is None or update.effective_user is None:
        return
    if len(args) < 2:
        await update.message.reply_html(
            "Uso: <code>/signals run &lt;preset&gt; &lt;BTC/USDT,ETH/USDT&gt;</code>"
        )
        return

    preset_raw, tickers_raw = args[0].lower(), args[1]
    if preset_raw not in _VALID_PRESETS:
        await update.message.reply_html(
            f"Preset inválido: <code>{esc(preset_raw)}</code>. "
            f"Usa: {', '.join(sorted(_VALID_PRESETS))}."
        )
        return
    tickers = [t.strip() for t in tickers_raw.split(",") if t.strip()]
    if not tickers:
        await update.message.reply_html("⚠️ Lista de tickers vacía.")
        return

    chat_id = update.effective_user.id
    await update.message.reply_html(
        f"Escaneando <b>{esc(preset_raw)}</b> en {len(tickers)} tickers…"
    )
    try:
        count = await scanner_to_signals_job(
            context.application,
            preset=cast(Literal["oversold", "breakout", "trending"], preset_raw),
            tickers=tickers,
            notify_chat_id=chat_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("/signals run failed: {}", exc)
        await update.message.reply_html("⚠️ Error ejecutando el scan.")
        return

    if count == 0:
        await update.message.reply_html(
            "Sin signals nuevas. Nada que aprobar."
        )


# ─── Callback dispatcher ──────────────────────────────────────────

async def on_signal_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE  # noqa: ARG001
) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "sig":
        return
    action = parts[1]
    try:
        signal_id = int(parts[2])
    except ValueError:
        return

    if action == "ok":
        await _consume_signal(update, signal_id)
    elif action == "no":
        await _cancel_signal(update, signal_id)
    elif action == "chart":
        await _send_chart(update, signal_id)


def _actor_for(update: Update) -> str:
    """Build the audit ``actor`` string from a Telegram update."""
    user = update.effective_user
    if user is None:
        return "user:unknown"
    return f"user:{user.id}"


async def _consume_signal(update: Update, signal_id: int) -> None:
    query = update.callback_query
    if query is None:
        return
    repo = get_signal_repository()
    decision_repo = get_risk_decision_repository()

    # Step 1: ensure we have a fresh RiskDecision. If the latest one
    # is older than ``_DECISION_STALE_AFTER``, re-evaluate before
    # confirming the operator's approval.
    decision = await decision_repo.latest_for_signal(signal_id)
    if decision is None or _decision_is_stale(decision):
        persisted = await repo.get(signal_id)
        if persisted is None:
            await query.edit_message_text(
                f"⚠️ Signal #{signal_id} no encontrada.", parse_mode="HTML"
            )
            return
        try:
            snapshot = await get_portfolio_state().snapshot()
            initial = await get_risk_manager().evaluate(persisted, snapshot)

            def _factory(version: int, _d: RiskDecision = initial) -> RiskDecision:
                return replace(_d, version=version)

            decision = await decision_repo.append_with_retry(
                signal_id, _factory
            )
            logger.info(
                "sig:ok: re-evaluated stale decision for signal_id={} v={}",
                signal_id,
                decision.version,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sig:ok: re-evaluation failed for {}: {}", signal_id, exc)
            await query.edit_message_text(
                f"⚠️ No pude re-evaluar la signal #{signal_id}: {esc(str(exc))}",
                parse_mode="HTML",
            )
            return

    # Step 2: if the (possibly re-evaluated) decision is now rejected,
    # surface the reason and refuse to transition.
    if not decision.approved:
        await query.edit_message_text(
            f"⚠️ La re-evaluación rechazó la signal:\n\n{esc(decision.reasoning)}",
            parse_mode="HTML",
            reply_markup=_terminal_keyboard(),
        )
        return

    # Step 3: transition with audit + sized_amount metadata.
    metadata: dict[str, object] = {
        "risk_decision_version": decision.version,
    }
    if decision.sized_amount is not None:
        metadata["sized_amount_eur"] = str(decision.sized_amount)

    try:
        updated = await repo.transition(
            signal_id,
            "consumed",
            actor=_actor_for(update),
            event_type="approved",
            expected_from_status="pending",
            metadata=metadata,
        )
    except StaleSignalStateError as exc:
        await query.edit_message_text(
            f"⚠️ Signal #{signal_id} ya no está pendiente "
            f"(estado actual: {esc(exc.actual)}).",
            parse_mode="HTML",
        )
        return
    if updated is None:
        await query.edit_message_text(
            f"⚠️ Signal #{signal_id} no encontrada.", parse_mode="HTML"
        )
        return
    body = fmt_signal_card(updated)
    body += (
        f"\n\n✅ <b>Aprobada</b> — sized "
        f"<code>{esc(decision.sized_amount)} EUR</code> "
        f"(decision v{decision.version}). Pendiente de ejecución (FASE 9)."
    )
    await query.edit_message_text(
        body, parse_mode="HTML", reply_markup=_terminal_keyboard()
    )


def _decision_is_stale(decision: RiskDecision) -> bool:
    decided = decision.decided_at
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=UTC)
    return (datetime.now(UTC) - decided) > _DECISION_STALE_AFTER


async def _cancel_signal(update: Update, signal_id: int) -> None:
    query = update.callback_query
    if query is None:
        return
    repo = get_signal_repository()
    try:
        updated = await repo.transition(
            signal_id,
            "cancelled",
            actor=_actor_for(update),
            event_type="cancelled",
            expected_from_status="pending",
        )
    except StaleSignalStateError as exc:
        await query.edit_message_text(
            f"⚠️ Signal #{signal_id} ya no está pendiente "
            f"(estado actual: {esc(exc.actual)}).",
            parse_mode="HTML",
        )
        return
    if updated is None:
        await query.edit_message_text(
            f"⚠️ Signal #{signal_id} no encontrada.", parse_mode="HTML"
        )
        return
    body = fmt_signal_card(updated) + "\n\n❌ <b>Descartada</b>."
    await query.edit_message_text(
        body, parse_mode="HTML", reply_markup=_terminal_keyboard()
    )


async def _send_chart(update: Update, signal_id: int) -> None:
    """Ship a 4h candlestick PNG for the signal's ticker.

    Imports the market service lazily to avoid a hard dependency from
    the handlers package on ``mib.api.dependencies`` at module load
    time (matters for tests that import handlers in isolation).
    """
    from mib.api.dependencies import get_market_service  # noqa: PLC0415

    query = update.callback_query
    if query is None:
        return
    msg = query.message if isinstance(query.message, Message) else None
    chat_id = msg.chat_id if msg else (query.from_user.id if query.from_user else None)
    if chat_id is None:
        return

    repo = get_signal_repository()
    persisted = await repo.get(signal_id)
    if persisted is None:
        return
    ticker = persisted.signal.ticker

    market: MarketService = get_market_service()
    try:
        data = await market.get_symbol(ticker, ohlcv_timeframe="4h", ohlcv_limit=120)
    except Exception as exc:  # noqa: BLE001
        logger.info("sig:chart {} fetch failed: {}", ticker, exc)
        return
    df = candles_dataframe([c.model_dump() for c in data.candles])
    if df.empty:
        return
    path = await render_candles_png(df, title=ticker, timeframe="4h")
    if path is None:
        return
    import contextlib  # noqa: PLC0415
    import os  # noqa: PLC0415

    try:
        with open(path, "rb") as f:
            await query.get_bot().send_photo(
                chat_id=chat_id,
                photo=f,
                caption=f"📊 <b>{esc(ticker)}</b> · 4h · Signal #{signal_id}",
                parse_mode="HTML",
            )
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _terminal_keyboard() -> InlineKeyboardMarkup:
    """Empty placeholder keyboard — keeps the message from showing the
    pending buttons after a status transition. We could remove the
    keyboard entirely with ``reply_markup=None`` but Telegram leaves a
    visible gap in some clients; an empty row collapses cleanly.
    """
    return InlineKeyboardMarkup([[]])
