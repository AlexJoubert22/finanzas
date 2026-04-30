"""Tests for :class:`RiskDecisionRepository` — append-only contract.

The repository is the only persistence path for :class:`RiskDecision`.
Per ROADMAP.md Parte 0 mandate, ``add()`` is INSERT-only and rejects
mismatched versions. Re-evaluating the same signal yields successive
rows (v1, v2, v3, ...). Concurrent appends serialize via the
``UNIQUE(signal_id, version)`` constraint; the
:meth:`append_with_retry` helper recovers automatically.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.db.session import async_session_factory
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.protocol import GateResult
from mib.trading.risk.repo import (
    RiskDecisionRepository,
    RiskDecisionVersionMismatchError,
)
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _decision(*, signal_id: int, version: int, approved: bool = True) -> RiskDecision:
    return RiskDecision(
        signal_id=signal_id,
        version=version,
        approved=approved,
        gate_results=(
            GateResult(passed=True, reason="kill switch open", gate_name="kill_switch"),
        ),
        reasoning="test decision",
        decided_at=datetime.now(UTC),
        sized_amount=None,
    )


@pytest.fixture
def repo() -> RiskDecisionRepository:
    return RiskDecisionRepository(async_session_factory)


async def _seed_signal() -> int:
    """Persist a signal so the FK constraint on risk_decisions has a valid target."""
    s_repo = SignalRepository(async_session_factory)
    persisted = await s_repo.add(_signal())
    return persisted.id


@pytest.mark.asyncio
async def test_add_first_version_writes_row(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    row_id = await repo.add(_decision(signal_id=sid, version=1))
    assert row_id > 0


@pytest.mark.asyncio
async def test_add_rejects_version_mismatch(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Trying to insert v=1 when nothing exists is fine; v=2 first is bad."""
    sid = await _seed_signal()
    with pytest.raises(RiskDecisionVersionMismatchError) as exc:
        await repo.add(_decision(signal_id=sid, version=2))
    assert exc.value.expected == 1
    assert exc.value.actual == 2


@pytest.mark.asyncio
async def test_consecutive_versions_append(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    await repo.add(_decision(signal_id=sid, version=1))
    await repo.add(_decision(signal_id=sid, version=2))
    await repo.add(_decision(signal_id=sid, version=3))

    rows = await repo.list_for_signal(sid)
    assert [d.version for d in rows] == [1, 2, 3]


@pytest.mark.asyncio
async def test_re_evaluation_with_same_version_rejected(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    await repo.add(_decision(signal_id=sid, version=1))
    with pytest.raises(RiskDecisionVersionMismatchError):
        await repo.add(_decision(signal_id=sid, version=1))


@pytest.mark.asyncio
async def test_next_version_for_starts_at_1(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    assert await repo.next_version_for(sid) == 1
    await repo.add(_decision(signal_id=sid, version=1))
    assert await repo.next_version_for(sid) == 2


@pytest.mark.asyncio
async def test_latest_for_signal_returns_highest_version(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    await repo.add(_decision(signal_id=sid, version=1, approved=True))
    await repo.add(_decision(signal_id=sid, version=2, approved=False))
    latest = await repo.latest_for_signal(sid)
    assert latest is not None
    assert latest.version == 2
    assert latest.approved is False


@pytest.mark.asyncio
async def test_latest_for_signal_returns_none_when_no_decisions(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    assert await repo.latest_for_signal(999) is None


@pytest.mark.asyncio
async def test_append_with_retry_persists(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()

    def factory(version: int) -> RiskDecision:
        return _decision(signal_id=sid, version=version)

    decision = await repo.append_with_retry(sid, factory)
    assert decision.version == 1
    assert (await repo.next_version_for(sid)) == 2


@pytest.mark.asyncio
async def test_append_with_retry_validates_factory_signal_id(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Factory must produce a decision matching the signal_id arg."""
    sid = await _seed_signal()

    def bad_factory(version: int) -> RiskDecision:
        return _decision(signal_id=sid + 1, version=version)

    with pytest.raises(ValueError, match="signal_id"):
        await repo.append_with_retry(sid, bad_factory)


@pytest.mark.asyncio
async def test_concurrent_appends_both_persist_with_retry(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """Two simultaneous append_with_retry on the same signal: both succeed
    (versions 1 and 2). The repo's UNIQUE constraint guarantees no
    duplicate version; the retry helper picks up the new next_version
    on the second attempt of the loser.
    """
    sid = await _seed_signal()

    def make_factory(version: int) -> RiskDecision:
        return _decision(signal_id=sid, version=version)

    a, b = await asyncio.gather(
        repo.append_with_retry(sid, make_factory),
        repo.append_with_retry(sid, make_factory),
    )
    versions = sorted([a.version, b.version])
    assert versions == [1, 2]
    rows = await repo.list_for_signal(sid)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_decimal_sized_amount_roundtrips(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    decision = RiskDecision(
        signal_id=sid,
        version=1,
        approved=True,
        gate_results=(),
        reasoning="sized",
        decided_at=datetime.now(UTC),
        sized_amount=Decimal("123.45000000"),
    )
    await repo.add(decision)
    fetched = await repo.latest_for_signal(sid)
    assert fetched is not None
    assert fetched.sized_amount == Decimal("123.45000000")


@pytest.mark.asyncio
async def test_gate_results_roundtrip_through_json(
    repo: RiskDecisionRepository, fresh_db: None  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    decision = RiskDecision(
        signal_id=sid,
        version=1,
        approved=False,
        gate_results=(
            GateResult(passed=True, reason="kill open", gate_name="kill_switch"),
            GateResult(
                passed=False, reason="DD breached", gate_name="daily_drawdown"
            ),
        ),
        reasoning="rejected by daily_drawdown",
        decided_at=datetime.now(UTC),
    )
    await repo.add(decision)
    fetched = await repo.latest_for_signal(sid)
    assert fetched is not None
    assert len(fetched.gate_results) == 2
    assert fetched.gate_results[1].gate_name == "daily_drawdown"
    assert fetched.gate_results[1].passed is False
