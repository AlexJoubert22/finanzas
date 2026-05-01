"""Tests for :class:`AIValidationRepository` (FASE 11.5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mib.ai.models import TaskType
from mib.db.models import AIValidationRow
from mib.db.session import async_session_factory
from mib.trading.ai_validations_repo import (
    AIValidationRepository,
    derive_request_hash,
)
from mib.trading.ai_validator import AIValidationResult
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=63_000.0,
        target_2=66_000.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _result(
    *,
    success: bool = True,
    approve: bool = True,
    confidence: str = "0.8",
    provider: str = "nvidia",
    raw: str = '{"approve": true, "confidence": 0.8}',
) -> AIValidationResult:
    return AIValidationResult(
        success=success,
        approve=approve,
        confidence=Decimal(confidence),
        concerns=("aligned",) if approve else ("misaligned",),
        size_modifier=Decimal("1.0"),
        rationale_short="ok",
        provider_used=provider,
        model_used="r1",
        latency_ms=42,
        raw_response=raw,
    )


async def _seed_signal_id() -> int:
    repo = SignalRepository(async_session_factory)
    persisted = await repo.add(_signal())
    return persisted.id


# ─── derive_request_hash ────────────────────────────────────────────


def test_derive_request_hash_deterministic() -> None:
    a = derive_request_hash(1, "abc")
    b = derive_request_hash(1, "abc")
    assert a == b
    assert len(a) == 16


def test_derive_request_hash_changes_with_inputs() -> None:
    a = derive_request_hash(1, "abc")
    b = derive_request_hash(2, "abc")
    c = derive_request_hash(1, "def")
    assert a != b
    assert a != c


# ─── add() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_persists_full_row(fresh_db: None) -> None:  # noqa: ARG001
    sid = await _seed_signal_id()
    repo = AIValidationRepository(async_session_factory)
    new_id = await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(),
        request_hash="abc1234567890def",
        decided_at=_now(),
    )
    assert new_id > 0
    async with async_session_factory() as session:
        row = await session.get(AIValidationRow, new_id)
        assert row is not None
        assert row.signal_id == sid
        assert row.task_type == "trade_validate"
        assert row.provider_used == "nvidia"
        assert row.approve is True
        assert row.confidence == Decimal("0.8")
        assert row.success is True
        assert row.response_json == {"approve": True, "confidence": 0.8}
        assert row.request_hash == "abc1234567890def"


@pytest.mark.asyncio
async def test_add_failed_validation_persists_with_error(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal_id()
    repo = AIValidationRepository(async_session_factory)
    failed = AIValidationResult(
        success=False,
        approve=False,
        confidence=Decimal(0),
        concerns=("router_failed",),
        size_modifier=Decimal("1.0"),
        rationale_short="all providers exhausted",
        provider_used="",
        model_used="",
        latency_ms=0,
        error="router_failed",
        raw_response="",
    )
    new_id = await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=failed,
        request_hash="hashfail",
        decided_at=_now(),
    )
    async with async_session_factory() as session:
        row = await session.get(AIValidationRow, new_id)
        assert row is not None
        assert row.success is False
        assert row.error_message == "router_failed"
        assert row.provider_used is None  # empty string normalised
        assert row.response_json is None


@pytest.mark.asyncio
async def test_add_normalises_non_object_raw_response(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Provider returns a JSON array or non-JSON string → wrapped as {raw: ...}."""
    sid = await _seed_signal_id()
    repo = AIValidationRepository(async_session_factory)

    # Non-JSON raw text.
    new_id = await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(raw="not json at all"),
        request_hash="rawtext",
        decided_at=_now(),
    )
    async with async_session_factory() as session:
        row = await session.get(AIValidationRow, new_id)
        assert row is not None
        assert row.response_json == {"raw": "not json at all"}

    # JSON array (not object).
    array_raw = json.dumps([1, 2, 3])
    new_id_2 = await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(raw=array_raw),
        request_hash="rawarr",
        decided_at=_now(),
    )
    async with async_session_factory() as session:
        row2 = await session.get(AIValidationRow, new_id_2)
        assert row2 is not None
        assert row2.response_json == {"raw": array_raw}


# ─── Reads ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latest_for_signal_returns_most_recent(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal_id()
    repo = AIValidationRepository(async_session_factory)
    base = _now() - timedelta(hours=2)
    await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(provider="nvidia"),
        request_hash="first",
        decided_at=base,
    )
    await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(provider="openrouter"),
        request_hash="second",
        decided_at=base + timedelta(minutes=30),
    )
    latest = await repo.latest_for_signal(sid)
    assert latest is not None
    assert latest.request_hash == "second"
    assert latest.provider_used == "openrouter"


@pytest.mark.asyncio
async def test_latest_for_signal_unknown_returns_none(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = AIValidationRepository(async_session_factory)
    assert await repo.latest_for_signal(9999) is None


@pytest.mark.asyncio
async def test_list_recent_for_provider_filters(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal_id()
    repo = AIValidationRepository(async_session_factory)
    base = _now() - timedelta(hours=1)
    await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(provider="nvidia"),
        request_hash="nv1",
        decided_at=base,
    )
    await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(provider="groq"),
        request_hash="gq1",
        decided_at=base + timedelta(minutes=10),
    )
    await repo.add(
        signal_id=sid,
        task_type=TaskType.TRADE_VALIDATE,
        result=_result(provider="nvidia"),
        request_hash="nv2",
        decided_at=base + timedelta(minutes=20),
    )

    nvidia_rows = await repo.list_recent_for_provider("nvidia", limit=10)
    assert [r.request_hash for r in nvidia_rows] == ["nv2", "nv1"]
    groq_rows = await repo.list_recent_for_provider("groq", limit=10)
    assert [r.request_hash for r in groq_rows] == ["gq1"]
