"""Tests for the /heartbeat endpoint (FASE 13.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from mib.api.app import create_app
from mib.config import get_settings
from mib.observability.scheduler_health import (
    _reset_for_tests,
    get_scheduler_health,
)


@pytest.fixture(autouse=True)
def _reset_health() -> None:
    _reset_for_tests()


@pytest.fixture
def _enable_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Patch the heartbeat_token setting through the cached singleton."""
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_token", "test-secret")
    return "test-secret"


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty heartbeat_token → endpoint responds 503 'dead-man disabled'."""
    settings = get_settings()
    monkeypatch.setattr(settings, "heartbeat_token", "")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=anything")
    assert r.status_code == 503
    assert "disabled" in r.text


@pytest.mark.asyncio
async def test_heartbeat_bad_token_returns_401(_enable_token: str) -> None:  # noqa: ARG001
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=wrong")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_no_tick_yet_returns_stalled(
    _enable_token: str,  # noqa: ARG001
) -> None:
    """Cold-start: scheduler hasn't ticked yet → 503."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=test-secret")
    assert r.status_code == 503
    assert "scheduler" in r.json()["reason"]


@pytest.mark.asyncio
async def test_heartbeat_ok_when_recent_tick_and_recon(
    _enable_token: str,  # noqa: ARG001
) -> None:
    health = get_scheduler_health()
    now = datetime.now(UTC).replace(tzinfo=None)
    health.last_tick_at = now - timedelta(seconds=10)
    health.last_reconcile_at = now - timedelta(seconds=30)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=test-secret")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "ts" in body


@pytest.mark.asyncio
async def test_heartbeat_stalled_when_scheduler_old(
    _enable_token: str,  # noqa: ARG001
) -> None:
    health = get_scheduler_health()
    health.last_tick_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=120
    )
    health.last_reconcile_at = datetime.now(UTC).replace(tzinfo=None)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=test-secret")
    assert r.status_code == 503
    assert "scheduler" in r.json()["reason"]


@pytest.mark.asyncio
async def test_heartbeat_stalled_when_reconcile_old(
    _enable_token: str,  # noqa: ARG001
) -> None:
    health = get_scheduler_health()
    now = datetime.now(UTC).replace(tzinfo=None)
    health.last_tick_at = now - timedelta(seconds=5)  # tick recent
    health.last_reconcile_at = now - timedelta(seconds=1200)  # 20 min stale
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=test-secret")
    assert r.status_code == 503
    assert "reconcile" in r.json()["reason"]


@pytest.mark.asyncio
async def test_heartbeat_response_minimalist(
    _enable_token: str,  # noqa: ARG001
) -> None:
    """No leakage: ok response has only status + ts."""
    health = get_scheduler_health()
    now = datetime.now(UTC).replace(tzinfo=None)
    health.last_tick_at = now
    health.last_reconcile_at = now
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/heartbeat?token=test-secret")
    body = r.json()
    assert set(body.keys()) == {"status", "ts"}
