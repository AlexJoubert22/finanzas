"""Coordinator job: StrategyEngine → SignalRepository → RiskManager → Telegram.

The strategy engine is pure (returns ``list[Signal]``). The repository
persists. This module is the glue:

1. Run the engine over the universe → ``list[Signal]``.
2. Persist each signal (FASE 7.5).
3. Evaluate risk for each persisted signal — gates + sizer (FASE 8.6).
4. Persist the :class:`RiskDecision` append-only.
5. Ship a Telegram card. If the decision is approved, the card shows
   the proposed sized amount and approval buttons. If rejected, the
   card shows the rejection reason and no buttons (nothing to approve).

**Telegram is best-effort.** Send failures log a warning and the
signal stays ``pending`` in the DB so ``/signals pending`` recovers
it later. We never roll back persistence because of a UI failure.
**Risk evaluation is also best-effort** for telemetry purposes —
a gate exception logs an error and the signal stays pending without
a decision row, surfaced to the operator on the next ``/signals
pending`` check.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from mib.api.dependencies import (
    get_portfolio_state,
    get_risk_decision_repository,
    get_risk_manager,
    get_signal_repository,
    get_strategy_engine,
)
from mib.logger import logger
from mib.services.scanner import PresetName
from mib.telegram.formatters import fmt_signal_with_decision
from mib.trading.risk.decision import RiskDecision

if TYPE_CHECKING:
    from mib.telegram import BotApp


def signal_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard for an emitted signal.

    ``callback_data`` is kept short (``sig:<action>:<id>``, ~9 bytes
    for ids up to 99 999) — well below Telegram's 64-byte cap. Using
    the int autoincrement id rather than a UUID is what keeps it cheap.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Aprobar", callback_data=f"sig:ok:{signal_id}"
                ),
                InlineKeyboardButton(
                    "❌ Descartar", callback_data=f"sig:no:{signal_id}"
                ),
                InlineKeyboardButton(
                    "📊 Chart", callback_data=f"sig:chart:{signal_id}"
                ),
            ]
        ]
    )


async def scanner_to_signals_job(
    app: BotApp,
    *,
    preset: PresetName,
    tickers: list[str],
    notify_chat_id: int,
) -> int:
    """Run the engine, persist hits, evaluate risk, fire Telegram.

    Returns the number of signals persisted (regardless of how many
    Telegram messages succeeded — the user may want to inspect the DB
    even if the chat is offline).
    """
    engine = get_strategy_engine()
    repo = get_signal_repository()
    risk_manager = get_risk_manager()
    decision_repo = get_risk_decision_repository()
    portfolio_state = get_portfolio_state()

    signals = await engine.run(preset, tickers)
    if not signals:
        logger.debug(
            "scanner_to_signals: preset={} produced 0 signals over {} tickers",
            preset,
            len(tickers),
        )
        return 0

    persisted_count = 0
    for sig in signals:
        try:
            persisted = await repo.add(sig)
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.warning(
                "scanner_to_signals: persist failed for {}: {}", sig.ticker, exc
            )
            continue
        persisted_count += 1

        # ── Risk evaluation (FASE 8.6) ──────────────────────────────
        decision: RiskDecision | None = None
        try:
            snapshot = await portfolio_state.snapshot()
            # The manager doesn't know about persistence versions;
            # ``append_with_retry`` computes the next version and we
            # re-stamp the decision via ``dataclasses.replace`` inside
            # the factory so the retry loop can adjust on race.
            initial = await risk_manager.evaluate(persisted, snapshot)

            def _factory(version: int, _d: RiskDecision = initial) -> RiskDecision:
                return replace(_d, version=version)

            decision = await decision_repo.append_with_retry(
                persisted.id, _factory
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "scanner_to_signals: risk evaluation failed for #{}: {}",
                persisted.id,
                exc,
            )
            # Continue to send a Telegram message; the signal remains
            # 'pending' without a decision row, surfaced to the
            # operator on the next /signals pending check.

        # ── Telegram notify ────────────────────────────────────────
        try:
            text = fmt_signal_with_decision(persisted, decision)
            keyboard = signal_keyboard(persisted.id) if (
                decision is not None and decision.approved
            ) else None
            await app.bot.send_message(
                chat_id=notify_chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 — telegram is best-effort
            logger.warning(
                "scanner_to_signals: telegram notify failed for #{}: {} "
                "(signal stays pending in DB)",
                persisted.id,
                exc,
            )

    logger.info(
        "scanner_to_signals: preset={} persisted={}/{}",
        preset,
        persisted_count,
        len(signals),
    )
    return persisted_count
