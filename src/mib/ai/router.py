"""Multi-provider AI router with task-typed fallback chains.

Spec §5: route every ``AITask`` through an ordered list of
``(provider, model)`` candidates. On 429 / 5xx / timeout we drop to the
next one in chain. Every attempt (success or failure) is logged into
``ai_calls`` via the :class:`UsageTracker` so:

- The ``/health`` endpoint can report per-provider usage percentage.
- Providers close to their daily quota are auto-skipped.

Per spec §12, this module is subject to ``mypy --strict``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mib.ai.models import (
    GEMINI_FLASH,
    GEMINI_FLASH_LITE,
    GROQ_8B,
    GROQ_70B,
    OPENROUTER_ANALYSIS,
    OPENROUTER_FAST,
    OPENROUTER_REASONING,
    OPENROUTER_SUMMARY,
    ProviderId,
    TaskType,
)
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.ai.usage_tracker import UsageTracker
from mib.config import get_settings
from mib.logger import logger


@dataclass(frozen=True)
class ChainStep:
    provider: ProviderId
    model: str


# Fallback chains per TaskType (spec §5, adapted to April-2026 inventory).
# First entry = preferred, last = last resort.
FALLBACK_CHAINS: dict[TaskType, list[ChainStep]] = {
    TaskType.FAST_CLASSIFY: [
        ChainStep(ProviderId.GROQ, GROQ_8B),
        ChainStep(ProviderId.GEMINI, GEMINI_FLASH_LITE),
        ChainStep(ProviderId.OPENROUTER, OPENROUTER_FAST),
    ],
    TaskType.ANALYSIS: [
        ChainStep(ProviderId.GROQ, GROQ_70B),
        ChainStep(ProviderId.OPENROUTER, OPENROUTER_ANALYSIS),
        ChainStep(ProviderId.GEMINI, GEMINI_FLASH),
    ],
    TaskType.REASONING: [
        ChainStep(ProviderId.OPENROUTER, OPENROUTER_REASONING),
        ChainStep(ProviderId.GEMINI, GEMINI_FLASH),
        ChainStep(ProviderId.GROQ, GROQ_70B),
    ],
    TaskType.SUMMARY: [
        ChainStep(ProviderId.GEMINI, GEMINI_FLASH_LITE),
        ChainStep(ProviderId.GROQ, GROQ_8B),
        ChainStep(ProviderId.OPENROUTER, OPENROUTER_SUMMARY),
    ],
}


class AIRouter:
    """Pick the first provider in the chain that is available and not over quota."""

    def __init__(
        self,
        providers: dict[ProviderId, AIProvider],
        usage_tracker: UsageTracker | None = None,
    ) -> None:
        self._providers = providers
        self._usage = usage_tracker or UsageTracker()
        s = get_settings()
        self._daily_limits: dict[ProviderId, int] = {
            ProviderId.GROQ: s.groq_daily_limit,
            ProviderId.OPENROUTER: s.openrouter_daily_limit,
            ProviderId.GEMINI: s.gemini_daily_limit,
        }

    async def complete(self, task: AITask) -> AIResponse:
        """Run ``task`` through its fallback chain.

        Returns the first successful :class:`AIResponse`. If every step
        fails (or is skipped for quota / missing key), the returned
        ``AIResponse`` has ``success=False`` and ``error`` populated so
        the caller can render a degraded response without IA.
        """
        chain = FALLBACK_CHAINS.get(task.task_type)
        if not chain:
            return AIResponse.failed(
                provider=ProviderId.GROQ,
                model="",
                error=f"no fallback chain for task_type={task.task_type}",
            )

        last_error: str = "no provider attempted"
        for step in chain:
            provider = self._providers.get(step.provider)
            if provider is None or not provider.is_available():
                logger.debug(
                    "airouter: skip {} (provider unavailable)", step.provider.value
                )
                last_error = f"{step.provider.value}: provider unavailable"
                continue

            # Quota gate — skip if >=90% of daily limit used.
            limit = self._daily_limits.get(step.provider, 0)
            if limit > 0 and await self._usage.is_over_limit(
                step.provider, daily_limit=limit
            ):
                logger.info(
                    "airouter: skip {} (≥90% of daily quota {} used)",
                    step.provider.value,
                    limit,
                )
                last_error = f"{step.provider.value}: quota gate"
                continue

            logger.debug(
                "airouter: try {} {} for task={}", step.provider.value, step.model, task.task_type
            )
            resp = await provider.complete(task, model=step.model)

            # Log the attempt regardless of outcome.
            await self._usage.log_call(
                provider=step.provider,
                task_type=task.task_type.value,
                model=step.model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                latency_ms=resp.latency_ms,
                success=resp.success,
                error=resp.error or None,
            )

            if resp.success:
                return resp
            last_error = resp.error or "unknown"
            logger.info(
                "airouter: {} {} failed ({} ms): {} — trying next",
                step.provider.value,
                step.model,
                resp.latency_ms,
                last_error,
            )

        # Exhausted.
        return AIResponse(
            success=False,
            content="",
            provider=None,
            model="",
            error=f"all providers exhausted (last: {last_error})",
        )

    async def usage_snapshot(self) -> dict[str, float]:
        """Expose to /health."""
        return await self._usage.usage_snapshot(self._daily_limits)
