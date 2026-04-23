"""Unit tests for the AI router fallback chain.

Verifies that:
- Missing providers are skipped cleanly.
- On 429/5xx from the first provider the router falls through to the
  next (this is the criterion in spec FASE 4).
- A successful response is returned without consulting later steps.
- Every attempt is logged to the ``ai_calls`` DB table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from mib.ai.models import ProviderId, TaskType
from mib.ai.providers.base import AIProvider, AIResponse, AITask
from mib.ai.router import AIRouter
from mib.ai.usage_tracker import UsageTracker


class _StubProvider(AIProvider):
    """Deterministic provider for tests.

    The production ABC declares ``id: ClassVar[ProviderId]`` but for
    stubs we need a *per-instance* id so multiple stubs can coexist.
    Assigning on ``self`` shadows the class-level declaration for
    attribute lookups, which is what we want.
    """

    def __init__(self, pid: ProviderId, *, available: bool, outcomes: list[AIResponse]) -> None:
        self.id = pid  # type: ignore[misc]  # per-instance shadow of ClassVar
        self._available = available
        self._outcomes = outcomes
        self.calls = 0

    def is_available(self) -> bool:
        return self._available

    async def complete(self, task: AITask, *, model: str) -> AIResponse:
        i = min(self.calls, len(self._outcomes) - 1)
        self.calls += 1
        out = self._outcomes[i]
        # Fill in provider/model from the request so logging is realistic.
        return AIResponse(
            success=out.success,
            content=out.content,
            provider=self.id,
            model=model,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            latency_ms=out.latency_ms,
            error=out.error,
        )


@pytest.mark.asyncio
async def test_airouter_returns_first_successful(fresh_db: None) -> None:  # noqa: ARG001
    groq = _StubProvider(
        ProviderId.GROQ,
        available=True,
        outcomes=[AIResponse(success=True, content="ok-from-groq", latency_ms=50)],
    )
    opnr = _StubProvider(
        ProviderId.OPENROUTER, available=True, outcomes=[AIResponse(success=True, content="unused")]
    )
    gemi = _StubProvider(
        ProviderId.GEMINI, available=True, outcomes=[AIResponse(success=True, content="unused")]
    )
    router = AIRouter(
        {ProviderId.GROQ: groq, ProviderId.OPENROUTER: opnr, ProviderId.GEMINI: gemi}
    )

    resp = await router.complete(
        AITask(prompt="hello", task_type=TaskType.ANALYSIS, max_tokens=10)
    )

    assert resp.success is True
    assert resp.content == "ok-from-groq"
    assert groq.calls == 1
    # Should not have walked further down the chain.
    assert opnr.calls == 0
    assert gemi.calls == 0


@pytest.mark.asyncio
async def test_airouter_falls_through_on_429(fresh_db: None) -> None:  # noqa: ARG001
    """Acceptance criterion of FASE 4: force 429 on Groq → expect OpenRouter."""
    groq = _StubProvider(
        ProviderId.GROQ,
        available=True,
        outcomes=[
            AIResponse(success=False, error="429 Too Many Requests", latency_ms=20)
        ],
    )
    opnr = _StubProvider(
        ProviderId.OPENROUTER,
        available=True,
        outcomes=[AIResponse(success=True, content="from-openrouter", latency_ms=700)],
    )
    gemi = _StubProvider(
        ProviderId.GEMINI, available=True, outcomes=[AIResponse(success=True, content="unused")]
    )
    router = AIRouter(
        {ProviderId.GROQ: groq, ProviderId.OPENROUTER: opnr, ProviderId.GEMINI: gemi}
    )

    resp = await router.complete(
        AITask(prompt="analyse", task_type=TaskType.ANALYSIS, max_tokens=50)
    )

    assert resp.success is True
    assert resp.provider == ProviderId.OPENROUTER
    assert resp.content == "from-openrouter"
    assert groq.calls == 1
    assert opnr.calls == 1
    assert gemi.calls == 0


@pytest.mark.asyncio
async def test_airouter_skips_unavailable_providers(fresh_db: None) -> None:  # noqa: ARG001
    """Providers without keys must be skipped without logging a failure row."""
    groq = _StubProvider(
        ProviderId.GROQ,
        available=False,  # no key
        outcomes=[AIResponse(success=False)],
    )
    opnr = _StubProvider(
        ProviderId.OPENROUTER,
        available=True,
        outcomes=[AIResponse(success=True, content="or-ok", latency_ms=400)],
    )
    router = AIRouter({ProviderId.GROQ: groq, ProviderId.OPENROUTER: opnr})
    resp = await router.complete(
        AITask(prompt="p", task_type=TaskType.ANALYSIS)
    )
    assert resp.success is True
    assert resp.provider == ProviderId.OPENROUTER
    assert groq.calls == 0
    assert opnr.calls == 1


@pytest.mark.asyncio
async def test_airouter_all_providers_fail(fresh_db: None) -> None:  # noqa: ARG001
    groq = _StubProvider(
        ProviderId.GROQ, available=True, outcomes=[AIResponse(success=False, error="429")]
    )
    opnr = _StubProvider(
        ProviderId.OPENROUTER,
        available=True,
        outcomes=[AIResponse(success=False, error="503")],
    )
    gemi = _StubProvider(
        ProviderId.GEMINI,
        available=True,
        outcomes=[AIResponse(success=False, error="timeout")],
    )
    router = AIRouter(
        {ProviderId.GROQ: groq, ProviderId.OPENROUTER: opnr, ProviderId.GEMINI: gemi}
    )
    resp = await router.complete(AITask(prompt="p", task_type=TaskType.ANALYSIS))
    assert resp.success is False
    assert "exhausted" in resp.error


@pytest.mark.asyncio
async def test_airouter_logs_attempts_to_db(fresh_db: None) -> None:  # noqa: ARG001
    """Every attempt, success or failure, must append a row to ai_calls."""
    from mib.db.models import AICall
    from mib.db.session import async_session_factory

    groq = _StubProvider(
        ProviderId.GROQ,
        available=True,
        outcomes=[AIResponse(success=False, error="429", latency_ms=10)],
    )
    opnr = _StubProvider(
        ProviderId.OPENROUTER,
        available=True,
        outcomes=[AIResponse(success=True, content="done", latency_ms=100)],
    )
    router = AIRouter(
        {ProviderId.GROQ: groq, ProviderId.OPENROUTER: opnr},
        usage_tracker=UsageTracker(),
    )
    await router.complete(AITask(prompt="p", task_type=TaskType.ANALYSIS))

    async with async_session_factory() as session:
        rows = (await session.execute(select(AICall))).scalars().all()
    assert len(rows) == 2
    assert {r.provider for r in rows} == {"groq", "openrouter"}
    assert {r.success for r in rows} == {True, False}
