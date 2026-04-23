"""OpenRouter provider — uses the ``openai`` SDK with base_url override.

OpenRouter's chat-completions API is wire-compatible with OpenAI's, so
we reuse the official OpenAI SDK instead of pulling in yet another client.

Lazy import and client construction (FASE 5 pre-polish): import and
client build are deferred to the first call.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.config import get_settings
from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from openai import AsyncOpenAI


class OpenRouterProvider(AIProvider):
    id = ProviderId.OPENROUTER

    def __init__(self) -> None:
        self._key = get_settings().openrouter_api_key
        self._available = bool(self._key)
        self._client: AsyncOpenAI | None = None

    def is_available(self) -> bool:
        return self._available

    def _ensure_client(self) -> AsyncOpenAI | None:
        if self._client is not None:
            return self._client
        if not self._available:
            return None
        from openai import AsyncOpenAI  # noqa: PLC0415 - intentional lazy

        self._client = AsyncOpenAI(
            api_key=self._key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                # OpenRouter uses these headers to attribute traffic
                # to your app for their "apps" dashboard (optional).
                # MUST be ASCII-only: httpx enforces latin-1 for header
                # values (a `·` here produced UnicodeEncodeError).
                "HTTP-Referer": "https://bambuserverv2.local/mib",
                "X-Title": "MIB - Financial Intelligence Bot",
            },
        )
        return self._client

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        client = self._ensure_client()
        if client is None:
            return AIResponse.failed(
                provider=self.id, model=model, error="OPENROUTER_API_KEY not configured"
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
                timeout=25.0,  # OpenRouter free tier can be slow during peak hours
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("openrouter: {} failed: {}", model, exc)
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
