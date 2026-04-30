"""NVIDIA Build (NIM API) provider — OpenAI-compatible.

NVIDIA Build's chat-completions endpoint is wire-compatible with the
OpenAI SDK; we point the existing ``openai.AsyncOpenAI`` client at
``base_url`` from settings (default ``https://integrate.api.nvidia.com/v1``)
and pass the ``nvapi-…`` token as the API key.

Models exposed today (centralised in :mod:`mib.ai.models`):

- ``deepseek-ai/deepseek-r1`` — reasoning, primary on TaskType.REASONING
- ``nvidia/llama-3.3-nemotron-super-49b-v1`` — analysis, primary on ANALYSIS
- ``meta/llama-3.3-70b-instruct`` — fast/summary

Lazy import + client construction so the openai dependency is paid
only on first call.
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


class NvidiaProvider(AIProvider):
    id = ProviderId.NVIDIA

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.nvidia_api_key
        self._base_url = settings.nvidia_base_url
        self._available = bool(self._key)
        self._client: AsyncOpenAI | None = None

    def is_available(self) -> bool:
        return self._available

    def _ensure_client(self) -> AsyncOpenAI | None:
        if self._client is not None:
            return self._client
        if not self._available:
            return None
        from openai import AsyncOpenAI  # noqa: PLC0415 — intentional lazy

        self._client = AsyncOpenAI(api_key=self._key, base_url=self._base_url)
        return self._client

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        client = self._ensure_client()
        if client is None:
            return AIResponse.failed(
                provider=self.id, model=model, error="NVIDIA_API_KEY not configured"
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
                timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - start) * 1000)
            error = _classify_error(exc)
            logger.info("nvidia: {} failed: {} ({})", model, error, exc)
            return AIResponse.failed(
                provider=self.id, model=model, error=error, latency_ms=elapsed
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


def _classify_error(exc: BaseException) -> str:
    """Translate a raw exception into a stable error tag for AIResponse.

    The router uses the tag for log filtering and (future) per-error
    retry policy. Keep it short and stable across SDK versions.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return "timeout"
    # openai SDK raises RateLimitError / APIStatusError with status_code.
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429 or "rate limit" in msg or "ratelimit" in name:
        return "rate_limit"
    if isinstance(status, int) and 500 <= status < 600:
        return "upstream_5xx"
    if "5" in str(status or "")[:1] and len(str(status or "")) == 3:
        return "upstream_5xx"
    return f"error:{type(exc).__name__}"
