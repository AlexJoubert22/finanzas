"""Google Gemini provider — direct API via the new ``google-genai`` SDK.

We migrated from the deprecated ``google-generativeai`` SDK (phase 4
condition carried over from phase 1). Call site stays async-first by
running the sync client inside ``asyncio.to_thread`` — ``google-genai``
does expose an ``async`` API but some responses still block briefly on
tokenizer work, so threading is the safer default for now.
"""

from __future__ import annotations

import asyncio
import time

from google import genai

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.config import get_settings
from mib.logger import logger


class GeminiProvider(AIProvider):
    id = ProviderId.GEMINI

    def __init__(self) -> None:
        key = get_settings().gemini_api_key
        self._available = bool(key)
        # google-genai Client is lightweight and picks the API key from env
        # or from the explicit constructor argument.
        self._client = genai.Client(api_key=key) if self._available else None

    def is_available(self) -> bool:
        return self._available

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        if not self._available or self._client is None:
            return AIResponse.failed(
                provider=self.id, model=model, error="GEMINI_API_KEY not configured"
            )

        prompt = (
            f"{task.system}\n\n---\n{task.prompt}" if task.system else task.prompt
        )
        start = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._sync_generate, model, prompt, task),
                timeout=30.0,
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("gemini: {} timed out after 30s", model)
            return AIResponse.failed(
                provider=self.id, model=model, error="timeout", latency_ms=elapsed
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("gemini: {} failed: {}", model, exc)
            return AIResponse.failed(
                provider=self.id, model=model, error=str(exc), latency_ms=elapsed
            )

        elapsed = int((time.monotonic() - start) * 1000)
        content = getattr(resp, "text", "") or ""
        # Usage metadata may or may not be populated depending on the
        # model; we copy what we can.
        meta = getattr(resp, "usage_metadata", None)
        return AIResponse(
            success=True,
            content=content.strip(),
            provider=self.id,
            model=model,
            input_tokens=getattr(meta, "prompt_token_count", None),
            output_tokens=getattr(meta, "candidates_token_count", None),
            latency_ms=elapsed,
        )

    def _sync_generate(self, model: str, prompt: str, task: AITask) -> object:
        # The 'generate_content' method is the new-SDK equivalent of the
        # old google-generativeai's `model.generate_content`.
        assert self._client is not None
        return self._client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": task.temperature,
                "max_output_tokens": task.max_tokens,
            },
        )
