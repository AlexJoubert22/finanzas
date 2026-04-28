"""Unit tests for :class:`NvidiaProvider`.

Mocks the ``openai`` client at the instance level (no HTTP) — same
strategy used by other provider tests in this codebase. Covers:

- ``is_available()`` reflects API-key presence.
- Successful response is mapped to :class:`AIResponse(success=True)`.
- 429 → ``error="rate_limit"``.
- 5xx → ``error="upstream_5xx"``.
- Timeout → ``error="timeout"``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mib.ai.models import NVIDIA_FAST, ProviderId
from mib.ai.providers.base import AITask
from mib.ai.providers.nvidia_provider import NvidiaProvider
from mib.config import get_settings


def _enable_nvidia(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch settings to expose a non-empty API key for the provider."""
    monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
    settings = get_settings()
    monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test", raising=False)


def _fake_response(content: str = "ok") -> Any:
    """Mimic the chat-completion shape the openai SDK returns."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 7
    usage.completion_tokens = 3
    resp.usage = usage
    return resp


def _provider_with_client(client: Any) -> NvidiaProvider:
    """Return a NvidiaProvider with `_ensure_client` monkeypatched."""
    p = NvidiaProvider()
    p._available = True  # bypass key check for tests
    p._client = client
    return p


def test_is_available_false_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nvidia_api_key", "", raising=False)
    p = NvidiaProvider()
    assert p.is_available() is False


def test_is_available_true_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_nvidia(monkeypatch)
    p = NvidiaProvider()
    assert p.is_available() is True
    assert p.id is ProviderId.NVIDIA


@pytest.mark.asyncio
async def test_complete_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_nvidia(monkeypatch)
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_fake_response("hello from nvidia")
    )
    p = _provider_with_client(fake_client)

    resp = await p.complete(AITask(prompt="ping"), model=NVIDIA_FAST)
    assert resp.success is True
    assert resp.content == "hello from nvidia"
    assert resp.provider is ProviderId.NVIDIA
    assert resp.model == NVIDIA_FAST
    assert resp.input_tokens == 7
    assert resp.output_tokens == 3


@pytest.mark.asyncio
async def test_complete_no_key_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nvidia_api_key", "", raising=False)
    p = NvidiaProvider()
    resp = await p.complete(AITask(prompt="ping"), model=NVIDIA_FAST)
    assert resp.success is False
    assert "not configured" in (resp.error or "")


@pytest.mark.asyncio
async def test_complete_rate_limit_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_nvidia(monkeypatch)

    class FakeRateLimitError(Exception):
        status_code = 429

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=FakeRateLimitError("too many requests")
    )
    p = _provider_with_client(fake_client)

    resp = await p.complete(AITask(prompt="ping"), model=NVIDIA_FAST)
    assert resp.success is False
    assert resp.error == "rate_limit"


@pytest.mark.asyncio
async def test_complete_5xx_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_nvidia(monkeypatch)

    class FakeServerError(Exception):
        status_code = 503

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=FakeServerError("upstream gone")
    )
    p = _provider_with_client(fake_client)

    resp = await p.complete(AITask(prompt="ping"), model=NVIDIA_FAST)
    assert resp.success is False
    assert resp.error == "upstream_5xx"


@pytest.mark.asyncio
async def test_complete_timeout_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_nvidia(monkeypatch)

    class FakeTimeoutError(Exception):
        pass

    FakeTimeoutError.__name__ = "APITimeoutError"

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=FakeTimeoutError("read timed out")
    )
    p = _provider_with_client(fake_client)

    resp = await p.complete(AITask(prompt="ping"), model=NVIDIA_FAST)
    assert resp.success is False
    assert resp.error == "timeout"
