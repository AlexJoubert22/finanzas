"""Groq provider — direct REST via the ``groq`` official client.

Groq is our fastest option (their LPU backend returns in ~1 s for 70B
models) and has the most generous free-tier QPS; we prefer it for
task types ``FAST_CLASSIFY`` and ``ANALYSIS``.

Lazy import and client construction (FASE 5 pre-polish): we don't
import the ``groq`` SDK at module load time, and we don't build the
``AsyncGroq`` client until the first real call. This keeps the idle
baseline lower and lets ``/health`` query the router's quota snapshot
without touching any upstream.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.config import get_settings
from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from groq import AsyncGroq


class GroqProvider(AIProvider):
    id = ProviderId.GROQ

    def __init__(self) -> None:
        self._key = get_settings().groq_api_key
        self._available = bool(self._key)
        # Lazy: no SDK import, no client build until first call.
        self._client: AsyncGroq | None = None

    def is_available(self) -> bool:
        return self._available

    def _ensure_client(self) -> AsyncGroq | None:
        if self._client is not None:
            return self._client
        if not self._available:
            return None
        from groq import AsyncGroq  # noqa: PLC0415 - intentional lazy

        self._client = AsyncGroq(api_key=self._key)
        return self._client

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        client = self._ensure_client()
        if client is None:
            return AIResponse.failed(
                provider=self.id, model=model, error="GROQ_API_KEY not configured"
            )

        messages: list[dict[str, Any]] = []
        if task.system:
            messages.append({"role": "system", "content": task.system})
        messages.append({"role": "user", "content": task.prompt})

        start = time.monotonic()
        try:
            resp = await client.chat.completions.create(
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
