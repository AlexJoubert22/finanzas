"""Contract every concrete LLM provider obeys.

Each provider wraps *one* remote API (Groq, OpenRouter, Gemini). The
``AIRouter`` orchestrates them according to the fallback chain per
task type defined in :mod:`mib.ai.router`.

Per spec §12, this module is subject to ``mypy --strict``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from mib.ai.models import ProviderId, TaskType


@dataclass(frozen=True)
class AITask:
    """One LLM invocation request."""

    prompt: str
    system: str = ""
    task_type: TaskType = TaskType.FAST_CLASSIFY
    max_tokens: int = 512
    temperature: float = 0.3
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class AIResponse:
    """Uniform return shape regardless of provider.

    ``success=True`` means the provider returned a usable completion and
    ``content`` is set. ``success=False`` means the caller should either
    fall back to the next provider in chain (the :class:`AIRouter` does
    this) or return a degraded response without IA.
    """

    success: bool
    content: str = ""
    provider: ProviderId | None = None
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0
    error: str = ""

    @classmethod
    def failed(
        cls,
        *,
        provider: ProviderId,
        model: str,
        error: str,
        latency_ms: int = 0,
    ) -> AIResponse:
        return cls(
            success=False,
            provider=provider,
            model=model,
            error=error,
            latency_ms=latency_ms,
        )


class ProviderNotConfiguredError(Exception):
    """Raised at provider construction when no API key is available.

    The Router catches it so callers never see a crash when a given key
    is missing — they just get the next provider in the fallback chain
    (or success=False if nothing is configured at all).
    """


class AIProvider(ABC):
    """Base class for Groq/OpenRouter/Gemini providers."""

    #: Identifier stored in ``ai_calls.provider``.
    id: ProviderId

    @abstractmethod
    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        """Run ``task`` against the named model and return a normalised response.

        Implementations MUST NOT raise on transient/upstream errors —
        they should catch and return ``AIResponse.failed(...)`` so the
        router can fall through cleanly. They MAY raise on programmer
        errors (wrong model id, missing key when the provider thought
        it was configured).
        """

    @abstractmethod
    def is_available(self) -> bool:
        """True if this provider has enough config to attempt a call.

        A provider without its API key is skipped by the router.
        """
