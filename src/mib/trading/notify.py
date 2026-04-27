"""Coordinator job: StrategyEngine → SignalRepository → Telegram.

The strategy engine is pure (returns ``list[Signal]``). The repository
persists. This module is the glue: it runs both in order and then ships
each new row to Telegram with the approval keyboard.

**Telegram is best-effort.** If sending the notification fails (network
hiccup, Telegram down, bot blocked by user), the signal stays in the DB
with ``status='pending'`` so ``/signals pending`` recovers it later. We
never roll back persistence because of a UI failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from mib.api.dependencies import get_signal_repository, get_strategy_engine
from mib.logger import logger
from mib.services.scanner import PresetName
from mib.telegram.formatters import fmt_signal_card

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
    """Run the engine, persist hits, fire Telegram notifications.

    Returns the number of signals persisted (regardless of how many
    Telegram messages succeeded — the user may want to inspect the DB
    even if the chat is offline).
    """
    engine = get_strategy_engine()
    repo = get_signal_repository()

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

        try:
            await app.bot.send_message(
                chat_id=notify_chat_id,
                text=fmt_signal_card(persisted),
                parse_mode="HTML",
                reply_markup=signal_keyboard(persisted.id),
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
