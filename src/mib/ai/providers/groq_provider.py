"""Groq provider — direct REST via the ``groq`` official client.

Groq is our fastest option (their LPU backend returns in ~1 s for 70B
models) and has the most generous free-tier QPS; we prefer it for
task types ``FAST_CLASSIFY`` and ``ANALYSIS``.

If ``GROQ_API_KEY`` is empty we mark the provider as unavailable so
the router silently falls through to OpenRouter / Gemini.
"""

from __future__ import annotations

import time

from groq import AsyncGroq

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.config import get_settings
from mib.logger import logger


class GroqProvider(AIProvider):
    id = ProviderId.GROQ

    def __init__(self) -> None:
        key = get_settings().groq_api_key
        self._available = bool(key)
        self._client = AsyncGroq(api_key=key) if self._available else None

    def is_available(self) -> bool:
        return self._available

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        if not self._available or self._client is None:
            return AIResponse.failed(
                provider=self.id, model=model, error="GROQ_API_KEY not configured"
            )

        messages = []
        if task.system:
            messages.append({"role": "system", "content": task.system})
        messages.append({"role": "user", "content": task.prompt})

        start = time.monotonic()
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=task.max_tokens,
                temperature=task.temperature,
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001 - fall through to next provider
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("groq: {} failed: {}", model, exc)
            return AIResponse.failed(
                provider=self.id, model=model, error=str(exc), latency_ms=elapsed
            )

        elapsed = int((time.monotonic() - start) * 1000)
        content = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        return AIResponse(
            success=True,
            content=content,
            provider=self.id,
            model=model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=elapsed,
        )
