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
    get_ai_router,
    get_news_service,
    get_portfolio_state,
    get_risk_decision_repository,
    get_risk_manager,
    get_signal_repository,
    get_strategy_engine,
)
from mib.logger import logger
from mib.services.scanner import PresetName
from mib.telegram.formatters import fmt_signal_with_decision
from mib.trading.ai_validator import (
    AIValidationResult,
    TradeValidator,
    apply_size_modifier,
)
from mib.trading.risk.decision import RiskDecision
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal as SignalDC

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

    validator = TradeValidator(get_ai_router())

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

        # ── AI Trade Validator (FASE 11.2) ───────────────────────────
        # Runs BEFORE RiskManager. If approve=False or confidence
        # below floor, signal flips to 'ai_rejected' and we skip risk
        # evaluation entirely.
        validation: AIValidationResult | None = None
        try:
            validation = await _run_validator(validator, sig)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scanner_to_signals: validator crashed for #{}: {} "
                "(treating as ai_rejected)", persisted.id, exc,
            )

        if validation is not None and not validation.approve:
            await _mark_signal_ai_rejected(repo, persisted.id, validation)
            # No risk evaluation, no Telegram approval card. Continue
            # to the next signal without crashing.
            continue

        # ── Risk evaluation (FASE 8.6) ──────────────────────────────
        decision: RiskDecision | None = None
        try:
            snapshot = await portfolio_state.snapshot()
            # The manager doesn't know about persistence versions;
            # ``append_with_retry`` computes the next version and we
            # re-stamp the decision via ``dataclasses.replace`` inside
            # the factory so the retry loop can adjust on race.
            initial = await risk_manager.evaluate(persisted, snapshot)

            # Apply AI-issued size_modifier when the validator gave
            # us a successful approval (FASE 11.2).
            if validation is not None and validation.success:
                modified_sized = apply_size_modifier(
                    initial.sized_amount, validation.size_modifier
                )
                initial = replace(initial, sized_amount=modified_sized)

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


# ─── FASE 11.2 helpers ──────────────────────────────────────────────


async def _run_validator(
    validator: TradeValidator, signal: SignalDC
) -> AIValidationResult:
    """Build context strings and call the LLM validator.

    Macro / news context lookups stay defensive: every external call
    is wrapped so a downstream timeout never poisons the validation
    decision (the validator itself returns success=False on router
    failures, which the coordinator treats as ai_rejected).
    """
    macro_context = await _build_macro_context()
    news_context = await _build_news_context(signal.ticker)
    indicators_context = _format_indicators(signal.indicators)
    return await validator.validate(
        signal,
        macro_context=macro_context,
        news_context=news_context,
        indicators_context=indicators_context,
    )


async def _build_macro_context() -> str:
    """TODO FASE 28: pull from a real Macro snapshot service.

    Until then we emit a stable placeholder so the prompt's "macro
    context" slot still populates without inventing fake data.
    """
    return "(no macro snapshot available — placeholder until FASE 28)"


async def _build_news_context(ticker: str) -> str:
    """Last 3 news items for ``ticker`` with sentiment when available."""
    try:
        news = get_news_service()
        response = await news.for_ticker(ticker, limit=3)
        items = list(response.items)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "ai_validator: news fetch failed for {}: {}", ticker, exc
        )
        return "(news fetch unavailable)"
    if not items:
        return f"(no recent news for {ticker})"
    lines = []
    for n in items[:3]:
        title = getattr(n, "headline", "") or getattr(n, "title", "") or ""
        sentiment = getattr(n, "sentiment", None)
        published = getattr(n, "published_at", None)
        sent_str = f" [{sentiment}]" if sentiment else ""
        ts_str = f" ({published})" if published else ""
        lines.append(f"- {title}{sent_str}{ts_str}")
    return "\n".join(lines)


def _format_indicators(indicators: dict[str, float]) -> str:
    if not indicators:
        return "(no indicators)"
    return "\n".join(
        f"- {key}: {value}" for key, value in sorted(indicators.items())
    )


async def _mark_signal_ai_rejected(
    repo: SignalRepository, signal_id: int, validation: AIValidationResult
) -> None:
    """Transition the signal to ``status='ai_rejected'`` with audit
    metadata. Errors are swallowed (the signal stays 'pending' on
    failure; reconciler / operator can pick it up later).
    """
    try:
        await repo.transition(
            signal_id,
            "ai_rejected",
            actor="ai_validator",
            event_type="ai_rejected",
            reason=(validation.rationale_short or "ai_rejected")[:240],
            metadata={
                "approve": validation.approve,
                "confidence": str(validation.confidence),
                "concerns": list(validation.concerns),
                "size_modifier": str(validation.size_modifier),
                "provider_used": validation.provider_used,
                "model_used": validation.model_used,
                "warnings": list(validation.warnings),
                "success": validation.success,
            },
            expected_from_status="pending",
        )
        logger.info(
            "ai_validator: signal_id={} → ai_rejected (provider={}, "
            "confidence={}, concerns={}, success={})",
            signal_id,
            validation.provider_used,
            validation.confidence,
            len(validation.concerns),
            validation.success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ai_validator: failed to mark signal #{} ai_rejected: {}",
            signal_id, exc,
        )
