"""AI Trade Validator (FASE 11.2).

Wraps the LLM call between StrategyEngine and RiskManager. The
contract: takes a :class:`Signal`, the macro / news / indicators
contexts (in the order the prompt expects), and returns a
:class:`AIValidationResult` the coordinator in :mod:`mib.trading.notify`
uses to decide whether to feed the signal to RiskManager.

Rules (mirroring the prompt's anti-rubber-stamp guards):

- ``approve=False`` OR ``confidence < MIN_CONFIDENCE_FOR_APPROVE`` →
  the coordinator persists the signal as ``status='ai_rejected'`` and
  does NOT call RiskManager.
- ``approve=True`` AND ``confidence >= MIN_CONFIDENCE_FOR_APPROVE`` →
  the coordinator runs RiskManager and applies ``size_modifier`` to
  the sized amount before final emission.
- The validator NEVER raises. Provider failures, JSON parse errors,
  schema violations all surface via ``success=False`` so the caller
  can decide whether to fall back (default policy: degrade gracefully
  by treating the signal as ``ai_rejected``).

The actual ``ai_validations`` row persistence lands in FASE 11.5; this
module only produces the dataclass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from mib.ai.models import TaskType
from mib.ai.prompts import SYSTEM_TRADE_VALIDATOR_V1
from mib.ai.providers.base import AITask
from mib.ai.router import AIRouter
from mib.logger import logger
from mib.trading.signals import Signal

#: Below this confidence the validator's "approve" is overridden to
#: false. Mirrors the prompt's "default 0.5" behaviour.
MIN_CONFIDENCE_FOR_APPROVE: float = 0.5

#: Hard cap on size_modifier (matches the prompt's spec). Values that
#: come back outside [0.0, 1.5] get clamped + flagged in ``warnings``.
MAX_SIZE_MODIFIER: Decimal = Decimal("1.5")
MIN_SIZE_MODIFIER: Decimal = Decimal("0.0")


@dataclass(frozen=True)
class AIValidationResult:
    """Outcome of a single TRADE_VALIDATE call."""

    success: bool
    """False on provider exhaustion, JSON parse error, schema violation."""

    approve: bool
    """LLM-issued approval (already gated by ``confidence`` floor)."""

    confidence: Decimal
    """In ``[0.0, 1.0]``. Below :data:`MIN_CONFIDENCE_FOR_APPROVE`
    forces ``approve=False`` regardless of the LLM's claim."""

    concerns: tuple[str, ...]
    """Always at least 1 element on a successful validation. Empty
    means the prompt's anti-rubber-stamp guard failed and we
    auto-rejected the signal."""

    size_modifier: Decimal
    """In ``[0.0, 1.5]``. 1.0 = leave the sized amount unchanged.
    Clamped to range; clamping events listed in ``warnings``."""

    rationale_short: str

    provider_used: str
    """e.g. ``'nvidia'``. Empty on total failure."""

    model_used: str
    latency_ms: int
    error: str | None = None
    raw_response: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ─── Validator ──────────────────────────────────────────────────────


class TradeValidator:
    """Calls AIRouter with TRADE_VALIDATE and parses the strict-JSON reply."""

    def __init__(self, router: AIRouter) -> None:
        self._router = router

    async def validate(
        self,
        signal: Signal,
        *,
        macro_context: str,
        news_context: str,
        indicators_context: str,
    ) -> AIValidationResult:
        """Run the validation. Never raises.

        ``macro_context`` / ``news_context`` / ``indicators_context``
        are pre-formatted strings the coordinator builds. Order in the
        user-message matches the prompt's strict order: macro → news
        → indicators → signal.
        """
        user_message = _build_user_message(
            signal=signal,
            macro_context=macro_context,
            news_context=news_context,
            indicators_context=indicators_context,
        )
        task = AITask(
            task_type=TaskType.TRADE_VALIDATE,
            system=SYSTEM_TRADE_VALIDATOR_V1,
            prompt=user_message,
            temperature=0.0,
            max_tokens=512,
        )
        t0 = time.monotonic()
        response = await self._router.complete(task)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if not response.success:
            return AIValidationResult(
                success=False,
                approve=False,
                confidence=Decimal(0),
                concerns=("ai_router_failed",),
                size_modifier=Decimal(1),
                rationale_short=response.error or "router_failed",
                provider_used=(
                    response.provider.value if response.provider else ""
                ),
                model_used=response.model,
                latency_ms=response.latency_ms or latency_ms,
                error=response.error or "router_failed",
                raw_response=response.content,
            )

        return _parse_response_payload(
            content=response.content,
            provider_used=(
                response.provider.value if response.provider else ""
            ),
            model_used=response.model,
            latency_ms=response.latency_ms or latency_ms,
        )


# ─── Pure helpers (testable without an AIRouter) ────────────────────


def _build_user_message(
    *,
    signal: Signal,
    macro_context: str,
    news_context: str,
    indicators_context: str,
) -> str:
    """Compose the user-message in the prompt's strict order."""
    return (
        "MACRO CONTEXT (1):\n"
        f"{macro_context.strip() or '(no macro context provided)'}\n\n"
        "NEWS CONTEXT (2):\n"
        f"{news_context.strip() or '(no news context provided)'}\n\n"
        "TECHNICAL INDICATORS (3):\n"
        f"{indicators_context.strip() or '(no indicators provided)'}\n\n"
        "FINAL SIGNAL (4):\n"
        f"  ticker: {signal.ticker}\n"
        f"  side: {signal.side}\n"
        f"  strategy_id: {signal.strategy_id}\n"
        f"  timeframe: {signal.timeframe}\n"
        f"  entry_zone: [{signal.entry_zone[0]}, {signal.entry_zone[1]}]\n"
        f"  invalidation: {signal.invalidation}\n"
        f"  target_1: {signal.target_1}\n"
        f"  target_2: {signal.target_2}\n"
        f"  rationale: {signal.rationale}\n"
    )


def _parse_response_payload(
    *,
    content: str,
    provider_used: str,
    model_used: str,
    latency_ms: int,
) -> AIValidationResult:
    """Strict-JSON parse + schema validation + clamping.

    Errors don't raise: they collapse into ``success=False`` with the
    incident logged in ``error`` so the coordinator can degrade
    gracefully (treat as ai_rejected).
    """
    raw = (content or "").strip()
    # Strip markdown fences if a misbehaving provider added them.
    cleaned = _strip_markdown_fences(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning(
            "ai_validator: JSON parse failed (provider={}): {}",
            provider_used,
            exc,
        )
        return _failed_result(
            provider_used=provider_used,
            model_used=model_used,
            latency_ms=latency_ms,
            error=f"json_parse_error: {exc}",
            raw=raw,
        )
    if not isinstance(payload, dict):
        return _failed_result(
            provider_used=provider_used,
            model_used=model_used,
            latency_ms=latency_ms,
            error=f"json_not_object: {type(payload).__name__}",
            raw=raw,
        )

    warnings: list[str] = []
    approve_raw = payload.get("approve")
    if not isinstance(approve_raw, bool):
        return _failed_result(
            provider_used=provider_used,
            model_used=model_used,
            latency_ms=latency_ms,
            error="approve_not_bool",
            raw=raw,
        )

    confidence = _coerce_decimal(payload.get("confidence", 0.0))
    confidence = max(Decimal(0), min(Decimal(1), confidence))

    concerns_raw = payload.get("concerns") or []
    if not isinstance(concerns_raw, list) or not all(
        isinstance(c, str) for c in concerns_raw
    ):
        return _failed_result(
            provider_used=provider_used,
            model_used=model_used,
            latency_ms=latency_ms,
            error="concerns_not_list_of_strings",
            raw=raw,
        )
    concerns = tuple(c for c in concerns_raw if c.strip())

    # Anti-rubber-stamp: empty concerns -> auto-reject regardless of
    # what the LLM said about ``approve``.
    if not concerns:
        warnings.append("empty_concerns_auto_rejected")
        approve_raw = False
        confidence = min(confidence, Decimal("0.4"))

    size_modifier = _coerce_decimal(payload.get("size_modifier", 1.0))
    if size_modifier < MIN_SIZE_MODIFIER:
        warnings.append(f"size_modifier_below_min:{size_modifier}")
        size_modifier = MIN_SIZE_MODIFIER
    if size_modifier > MAX_SIZE_MODIFIER:
        warnings.append(f"size_modifier_above_max:{size_modifier}")
        size_modifier = MAX_SIZE_MODIFIER

    rationale_short = str(payload.get("rationale_short") or "")[:240]

    # Confidence floor: even when LLM said approve=True, sub-0.5
    # confidence forces approve=False.
    final_approve = approve_raw and confidence >= Decimal(
        str(MIN_CONFIDENCE_FOR_APPROVE)
    )
    if approve_raw and not final_approve:
        warnings.append(
            f"confidence_floor_failed:{confidence}<{MIN_CONFIDENCE_FOR_APPROVE}"
        )

    return AIValidationResult(
        success=True,
        approve=final_approve,
        confidence=confidence,
        concerns=concerns or ("empty_concerns",),
        size_modifier=size_modifier,
        rationale_short=rationale_short,
        provider_used=provider_used,
        model_used=model_used,
        latency_ms=latency_ms,
        error=None,
        raw_response=raw,
        warnings=tuple(warnings),
    )


def _failed_result(
    *,
    provider_used: str,
    model_used: str,
    latency_ms: int,
    error: str,
    raw: str,
) -> AIValidationResult:
    return AIValidationResult(
        success=False,
        approve=False,
        confidence=Decimal(0),
        concerns=("validation_failed",),
        size_modifier=Decimal(1),
        rationale_short=error[:240],
        provider_used=provider_used,
        model_used=model_used,
        latency_ms=latency_ms,
        error=error,
        raw_response=raw,
    )


def _coerce_decimal(value: Any) -> Decimal:
    """Tolerant numeric coercion for floats/ints/strings."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str) and value.strip():
        try:
            return Decimal(value)
        except Exception:  # noqa: BLE001
            return Decimal(0)
    return Decimal(0)


def _strip_markdown_fences(s: str) -> str:
    """Strip ```json``` / ``` fences if a provider misbehaves."""
    t = s.strip()
    if t.startswith("```"):
        # Drop first fence line.
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def apply_size_modifier(
    sized_amount_quote: Decimal | None, modifier: Decimal
) -> Decimal | None:
    """Multiply the sizer's output by the validator's modifier.

    Pure helper so notify.py can apply it after RiskManager produced
    the RiskDecision. Returns ``None`` unchanged if the input is None
    (sizing was skipped, e.g. signal rejected by gates).
    """
    if sized_amount_quote is None:
        return None
    multiplied = sized_amount_quote * modifier
    return multiplied.quantize(Decimal("0.00000001"))
