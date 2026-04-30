"""Tests for :class:`TradeValidator` (FASE 11.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mib.ai.models import ProviderId, TaskType
from mib.ai.providers.base import AIResponse, AITask
from mib.trading.ai_validator import (
    MAX_SIZE_MODIFIER,
    AIValidationResult,
    TradeValidator,
    _build_user_message,
    _parse_response_payload,
    _strip_markdown_fences,
    apply_size_modifier,
)
from mib.trading.signals import Signal


def _signal(side: str = "long") -> Signal:
    if side == "long":
        return Signal(
            ticker="BTC/USDT",
            side="long",
            strength=0.7,
            timeframe="1h",
            entry_zone=(60_000.0, 60_100.0),
            invalidation=58_800.0,
            target_1=61_200.0,
            target_2=63_600.0,
            rationale="oversold bounce",
            indicators={"rsi_14": 22.0, "atr_14": 800.0},
            generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            strategy_id="scanner.oversold.v1",
            confidence_ai=None,
        )
    return Signal(
        ticker="BTC/USDT",
        side="short",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_100.0),
        invalidation=61_200.0,
        target_1=58_800.0,
        target_2=56_400.0,
        rationale="overbought rejection",
        indicators={"rsi_14": 75.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.breakout.v1",
        confidence_ai=None,
    )


# ─── Pure helpers ───────────────────────────────────────────────────


def test_strip_markdown_fences_with_json_label() -> None:
    raw = '```json\n{"approve": true}\n```'
    assert _strip_markdown_fences(raw) == '{"approve": true}'


def test_strip_markdown_fences_no_fence_unchanged() -> None:
    assert _strip_markdown_fences('{"approve": true}') == '{"approve": true}'


def test_apply_size_modifier_one_unchanged() -> None:
    assert apply_size_modifier(Decimal("100"), Decimal("1.0")) == Decimal(
        "100.00000000"
    )


def test_apply_size_modifier_below_one_shrinks() -> None:
    assert apply_size_modifier(Decimal("100"), Decimal("0.6")) == Decimal(
        "60.00000000"
    )


def test_apply_size_modifier_above_one_grows() -> None:
    assert apply_size_modifier(Decimal("100"), Decimal("1.4")) == Decimal(
        "140.00000000"
    )


def test_apply_size_modifier_none_passthrough() -> None:
    """Sizing skipped (e.g. gate rejection) → modifier doesn't materialize a value."""
    assert apply_size_modifier(None, Decimal("1.0")) is None


def test_build_user_message_strict_order() -> None:
    sig = _signal("long")
    msg = _build_user_message(
        signal=sig,
        macro_context="bullish risk-on",
        news_context="positive earnings",
        indicators_context="rsi_14: 22",
    )
    # Strict ordering required by prompt: macro -> news -> indicators -> signal.
    macro_idx = msg.index("MACRO")
    news_idx = msg.index("NEWS")
    indicators_idx = msg.index("TECHNICAL")
    signal_idx = msg.index("FINAL SIGNAL")
    assert macro_idx < news_idx < indicators_idx < signal_idx
    assert "BTC/USDT" in msg
    assert "long" in msg


# ─── _parse_response_payload ────────────────────────────────────────


def _ok_payload(
    *,
    approve: bool = True,
    confidence: float = 0.8,
    concerns: list[str] | None = None,
    size_modifier: float = 1.0,
) -> str:
    return json.dumps(
        {
            "approve": approve,
            "confidence": confidence,
            "concerns": concerns
            if concerns is not None
            else ["aligned with macro", "rsi extreme"],
            "size_modifier": size_modifier,
            "rationale_short": "test",
        }
    )


def test_parse_happy_path() -> None:
    res = _parse_response_payload(
        content=_ok_payload(),
        provider_used="nvidia",
        model_used="r1",
        latency_ms=42,
    )
    assert res.success is True
    assert res.approve is True
    assert res.confidence == Decimal("0.8")
    assert len(res.concerns) == 2
    assert res.size_modifier == Decimal("1.0")
    assert res.provider_used == "nvidia"


def test_parse_empty_concerns_auto_rejects() -> None:
    """Anti-rubber-stamp: empty concerns flips approve to False."""
    res = _parse_response_payload(
        content=_ok_payload(approve=True, concerns=[]),
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.success is True
    assert res.approve is False  # auto-rejected
    assert "empty_concerns_auto_rejected" in res.warnings
    # Confidence is also clamped down.
    assert res.confidence <= Decimal("0.4")


def test_parse_low_confidence_overrides_approve() -> None:
    """confidence < 0.5 forces approve=False even if LLM said True."""
    res = _parse_response_payload(
        content=_ok_payload(approve=True, confidence=0.3),
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.success is True
    assert res.approve is False
    assert any("confidence_floor_failed" in w for w in res.warnings)


def test_parse_size_modifier_clamped_above_max() -> None:
    res = _parse_response_payload(
        content=_ok_payload(size_modifier=2.5),
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.size_modifier == MAX_SIZE_MODIFIER
    assert any("size_modifier_above_max" in w for w in res.warnings)


def test_parse_size_modifier_clamped_below_min() -> None:
    res = _parse_response_payload(
        content=_ok_payload(size_modifier=-0.5),
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.size_modifier == Decimal("0.0")
    assert any("size_modifier_below_min" in w for w in res.warnings)


def test_parse_invalid_json_returns_failure() -> None:
    res = _parse_response_payload(
        content="not json at all",
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.success is False
    assert res.approve is False
    assert res.error is not None
    assert "json_parse_error" in res.error


def test_parse_strips_markdown_fences() -> None:
    fenced = "```json\n" + _ok_payload() + "\n```"
    res = _parse_response_payload(
        content=fenced,
        provider_used="nvidia",
        model_used="r1",
        latency_ms=10,
    )
    assert res.success is True


def test_parse_approve_not_bool_returns_failure() -> None:
    bad = json.dumps(
        {
            "approve": "yes",
            "confidence": 0.8,
            "concerns": ["x"],
            "size_modifier": 1.0,
            "rationale_short": "t",
        }
    )
    res = _parse_response_payload(
        content=bad, provider_used="x", model_used="y", latency_ms=0
    )
    assert res.success is False
    assert res.error == "approve_not_bool"


# ─── End-to-end with stub router ────────────────────────────────────


class _StubRouter:
    """Mock AIRouter that returns canned responses."""

    def __init__(self, response: AIResponse) -> None:
        self._response = response
        self.calls: list[AITask] = []

    async def complete(self, task: AITask) -> AIResponse:
        self.calls.append(task)
        return self._response


@pytest.mark.asyncio
async def test_validator_routes_to_TRADE_VALIDATE_task_type() -> None:  # noqa: N802
    """The validator must request the TRADE_VALIDATE task type."""
    router = _StubRouter(
        AIResponse(
            success=True,
            content=_ok_payload(),
            provider=ProviderId.NVIDIA,
            model="r1",
            latency_ms=15,
        )
    )
    validator = TradeValidator(router)  # type: ignore[arg-type]
    res = await validator.validate(
        _signal("long"),
        macro_context="bullish",
        news_context="positive",
        indicators_context="rsi_14: 22",
    )
    assert len(router.calls) == 1
    assert router.calls[0].task_type == TaskType.TRADE_VALIDATE
    # System prompt is the v1 validator.
    assert "trading risk evaluator" in router.calls[0].system
    assert res.success is True
    assert res.approve is True


@pytest.mark.asyncio
async def test_validator_router_failure_surfaces_as_unsuccessful() -> None:
    router = _StubRouter(
        AIResponse(
            success=False,
            content="",
            provider=None,
            model="",
            error="all providers exhausted",
        )
    )
    validator = TradeValidator(router)  # type: ignore[arg-type]
    res = await validator.validate(
        _signal("long"),
        macro_context="",
        news_context="",
        indicators_context="",
    )
    assert res.success is False
    assert res.approve is False
    assert "exhausted" in (res.error or "")


@pytest.mark.asyncio
async def test_validator_logs_provider_used_when_router_succeeds() -> None:
    """Smoke for the per-provider observability the FASE 11 spec asks for."""
    router = _StubRouter(
        AIResponse(
            success=True,
            content=_ok_payload(),
            provider=ProviderId.OPENROUTER,
            model="reasoning-fallback",
            latency_ms=99,
        )
    )
    validator = TradeValidator(router)  # type: ignore[arg-type]
    res = await validator.validate(
        _signal("long"),
        macro_context="x",
        news_context="y",
        indicators_context="z",
    )
    assert res.provider_used == "openrouter"
    assert res.model_used == "reasoning-fallback"


# ─── Anti-rubber-stamp scenario asked by spec ───────────────────────


@pytest.mark.asyncio
async def test_long_signal_against_macro_news_indicators_rejects() -> None:
    """Spec test: long signal + bearish macro + bearish news + mixed
    indicators should produce confidence <=0.5 and approve=False.

    We simulate the LLM doing the right thing by returning a low
    confidence response. This locks in the contract that the
    validator believes the LLM (no rubber-stamp from our side).
    """
    payload = json.dumps(
        {
            "approve": False,
            "confidence": 0.3,
            "concerns": [
                "macro contradicts long",
                "news bearish on ticker",
                "indicators not confirmatory",
            ],
            "size_modifier": 0.0,
            "rationale_short": "directional misalignment across 3 contexts",
        }
    )
    router = _StubRouter(
        AIResponse(
            success=True,
            content=payload,
            provider=ProviderId.NVIDIA,
            model="r1",
            latency_ms=20,
        )
    )
    validator = TradeValidator(router)  # type: ignore[arg-type]
    res = await validator.validate(
        _signal("long"),
        macro_context="bearish risk-off, equities sold off, BTC dominance dropping",
        news_context="ticker hit by negative regulation news",
        indicators_context="macd bearish cross, rsi neutral, volume declining",
    )
    assert res.success is True
    assert res.approve is False
    assert res.confidence == Decimal("0.3")


@pytest.mark.asyncio
async def test_size_modifier_applied_to_sized_amount() -> None:
    """End-to-end of the spec example: 100€ * 0.6 → 60€."""
    payload = json.dumps(
        {
            "approve": True,
            "confidence": 0.75,
            "concerns": ["mild macro divergence"],
            "size_modifier": 0.6,
            "rationale_short": "directional match but partial alignment",
        }
    )
    router = _StubRouter(
        AIResponse(
            success=True,
            content=payload,
            provider=ProviderId.NVIDIA,
            model="r1",
            latency_ms=20,
        )
    )
    validator = TradeValidator(router)  # type: ignore[arg-type]
    res = await validator.validate(
        _signal("long"),
        macro_context="x",
        news_context="y",
        indicators_context="z",
    )
    assert res.success is True
    assert res.approve is True
    final_size = apply_size_modifier(Decimal("100"), res.size_modifier)
    assert final_size == Decimal("60.00000000")


# Suppress the unused-import warning for AIValidationResult.
_ = AIValidationResult
_ = Any
