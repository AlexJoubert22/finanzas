"""Tests for FASE 11.1 prompts + router chain wiring."""

from __future__ import annotations

from mib.ai.models import (
    NVIDIA_ANALYSIS,
    NVIDIA_REASONING,
    ProviderId,
    TaskType,
)
from mib.ai.prompts import (
    SYSTEM_NEWS_REACTION_V1,
    SYSTEM_TRADE_POSTMORTEM_V1,
    SYSTEM_TRADE_VALIDATOR_V1,
)
from mib.ai.router import FALLBACK_CHAINS

# ─── Prompt invariants ──────────────────────────────────────────────


def test_validator_prompt_includes_anti_rubber_stamp_clauses() -> None:
    """The validator must explicitly forbid empty concerns and demand
    confidence stay <=0.5 unless all 3 contexts align.
    """
    p = SYSTEM_TRADE_VALIDATOR_V1
    assert "concerns" in p
    assert "at least 1 element" in p
    assert "concerns=[]" in p  # explicit anti-pattern
    assert "Default confidence is 0.5" in p
    assert "above 0.7" in p
    # Imperative-rejection clause.
    assert "should I buy" in p


def test_validator_prompt_specifies_strict_json_schema() -> None:
    p = SYSTEM_TRADE_VALIDATOR_V1
    # All four required output fields must be named.
    for key in ("approve", "confidence", "concerns", "size_modifier", "rationale_short"):
        assert key in p
    # size_modifier range explicit.
    assert "[0.0, 1.5]" in p
    assert "STRICT JSON" in p


def test_postmortem_prompt_has_strict_schema_and_empty_batch_clause() -> None:
    p = SYSTEM_TRADE_POSTMORTEM_V1
    for key in (
        "patterns",
        "aggregate_pnl_quote",
        "outliers",
        "suggestions",
        "regime_summary",
    ):
        assert key in p
    # Empty-batch fallback explicitly documented.
    assert "no trades closed in window" in p
    assert "winner_pattern" in p
    assert "loser_pattern" in p


def test_news_reaction_prompt_has_three_decisions() -> None:
    p = SYSTEM_NEWS_REACTION_V1
    assert '"decision": "reduce" | "close" | "hold"' in p
    assert "STRICT JSON" in p
    # Justification length cap.
    assert "max 160 chars" in p


def test_disclaimer_present_in_all_v1_prompts() -> None:
    """Every prompt must end with the spec §5 disclaimer."""
    for p in (
        SYSTEM_TRADE_VALIDATOR_V1,
        SYSTEM_TRADE_POSTMORTEM_V1,
        SYSTEM_NEWS_REACTION_V1,
    ):
        assert "consejos financieros" in p


# ─── Router chain wiring ────────────────────────────────────────────


def test_trade_validate_chain_uses_reasoning_models() -> None:
    """TRADE_VALIDATE inherits the REASONING shape with NVIDIA first."""
    chain = FALLBACK_CHAINS[TaskType.TRADE_VALIDATE]
    assert len(chain) >= 3
    # First step: NVIDIA DeepSeek R1.
    assert chain[0].provider == ProviderId.NVIDIA
    assert chain[0].model == NVIDIA_REASONING
    # Chain spans all four providers (defensive: any one quota-out
    # still leaves 3 alternatives).
    providers = {step.provider for step in chain}
    assert providers == {
        ProviderId.NVIDIA,
        ProviderId.OPENROUTER,
        ProviderId.GEMINI,
        ProviderId.GROQ,
    }


def test_trade_postmortem_chain_uses_analysis_models() -> None:
    """TRADE_POSTMORTEM uses NVIDIA Nemotron 49B first (analysis-shaped)."""
    chain = FALLBACK_CHAINS[TaskType.TRADE_POSTMORTEM]
    assert len(chain) >= 3
    assert chain[0].provider == ProviderId.NVIDIA
    assert chain[0].model == NVIDIA_ANALYSIS
    providers = {step.provider for step in chain}
    assert providers == {
        ProviderId.NVIDIA,
        ProviderId.OPENROUTER,
        ProviderId.GEMINI,
        ProviderId.GROQ,
    }
