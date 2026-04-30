"""Canonical identifiers for every LLM we call.

Centralising model names in one place means a single-file change when
providers retire a model. The validation script
``scripts/validate_pandas_ta.py`` is the analogue for pandas-ta; this
module is the analogue for the LLM layer.

Mapping decided during FASE 4 (inventoried live on openrouter.ai
2026-04-23):

    Spec (original)                            → Replacement (current)
    deepseek/deepseek-chat-v3:free             → openai/gpt-oss-120b:free
    google/gemini-2.0-flash-exp:free           → google/gemma-3-27b-it:free
    meta-llama/llama-3.3-70b-instruct:free     → unchanged (still live)

Plus we add ``openai/gpt-oss-20b:free`` as the primary OpenRouter
fast-classify fallback (131 k context, ~quick, OK Spanish).
"""

from __future__ import annotations

from enum import StrEnum

# ─── Groq (direct API) ──────────────────────────────────────────────
GROQ_70B = "llama-3.3-70b-versatile"
GROQ_8B = "llama-3.1-8b-instant"


# ─── OpenRouter (``:free`` models, verified 2026-04-23) ────────────
OPENROUTER_REASONING = "openai/gpt-oss-120b:free"
OPENROUTER_ANALYSIS = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_FAST = "openai/gpt-oss-20b:free"
OPENROUTER_SUMMARY = "google/gemma-3-27b-it:free"


# ─── Google Gemini (direct API via google-genai, verified 2026-04-23) ──
# 15 RPM / ~1M tokens/day on free tier for both.
# Mapeo de modelos retirados:
#   gemini-1.5-flash-8b   → retirado (404), sustituido por 2.5-flash-lite.
#   gemini-2.0-flash-lite → "no longer available to new users" (nuestro tenant
#                            es posterior al cut-off), reemplazado por 2.5-flash-lite.
GEMINI_FLASH = "gemini-2.5-flash"
GEMINI_FLASH_LITE = "gemini-2.5-flash-lite"


# ─── NVIDIA Build (NIM API, OpenAI-compatible) ──────────────────────
# Operator subscription provides 1-year access. Slugs centralised here
# so a NVIDIA-side rename is a single-file change. Verified 2026-04-28.
NVIDIA_REASONING = "deepseek-ai/deepseek-r1"
NVIDIA_ANALYSIS = "nvidia/llama-3.3-nemotron-super-49b-v1"
NVIDIA_FAST = "meta/llama-3.3-70b-instruct"
NVIDIA_SUMMARY = "meta/llama-3.3-70b-instruct"


class TaskType(StrEnum):
    """Categories that drive the fallback chain in AIRouter."""

    FAST_CLASSIFY = "fast_classify"
    ANALYSIS = "analysis"
    REASONING = "reasoning"
    SUMMARY = "summary"
    # Trading-layer types reserved for FASE 11. Their fallback chains are
    # registered as empty placeholders so the enum + router API stay stable;
    # the router rejects calls against them until the chains get populated.
    TRADE_VALIDATE = "trade_validate"
    TRADE_POSTMORTEM = "trade_postmortem"


class ProviderId(StrEnum):
    """Short ids used in logs and in the ``ai_calls`` DB table."""

    GROQ = "groq"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    NVIDIA = "nvidia"
