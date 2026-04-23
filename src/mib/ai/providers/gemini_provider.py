"""Google Gemini provider — direct API via the new ``google-genai`` SDK.

We migrated from the deprecated ``google-generativeai`` SDK in FASE 4.
The sync client runs inside ``asyncio.to_thread`` — ``google-genai`` does
expose an ``async`` API but some responses still block briefly on
tokenizer work, so threading is the safer default for now.

Lazy import and client construction (FASE 5 pre-polish): ``google-genai``
brings grpcio + protobuf which add ~15 MiB to RSS. We defer everything
until the first real call.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from mib.ai.models import ProviderId
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.config import get_settings
from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from google.genai import Client


class GeminiProvider(AIProvider):
    id = ProviderId.GEMINI

    def __init__(self) -> None:
        self._key = get_settings().gemini_api_key
        self._available = bool(self._key)
        self._client: Client | None = None

    def is_available(self) -> bool:
        return self._available

    def _ensure_client(self) -> Client | None:
        if self._client is not None:
            return self._client
        if not self._available:
            return None
        from google import genai  # noqa: PLC0415 - intentional lazy

        self._client = genai.Client(api_key=self._key)
        return self._client

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        if self._ensure_client() is None:
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
        # `_ensure_client()` has already been called in `complete()`.
        client = self._client
        assert client is not None
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "temperature": task.temperature,
                "max_output_tokens": task.max_tokens,
            },
        )
