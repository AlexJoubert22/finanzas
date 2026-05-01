"""Tests for :func:`days_clean_streak` (FASE 13.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.session import async_session_factory
from mib.observability.clean_streak import (
    MAX_REPORTABLE_STREAK_DAYS,
    compute_days_clean_streak,
)
from mib.observability.incidents import (
    CriticalIncidentRepository,
    CriticalIncidentType,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_empty_table_returns_max(fresh_db: None) -> None:  # noqa: ARG001
    """Cold-start: no incidents ever -> MAX_REPORTABLE_STREAK_DAYS."""
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    assert days == MAX_REPORTABLE_STREAK_DAYS


@pytest.mark.asyncio
async def test_non_severe_quick_resolution_no_reset(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Non-severe + resolved within 24h does NOT reset the streak."""
    repo = CriticalIncidentRepository(async_session_factory)
    occurred = _now() - timedelta(days=10)
    pk = await repo.add(
        type_=CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL,
        occurred_at=occurred,
        auto_detected=True,
    )
    await repo.resolve_incident(
        pk,
        resolved_at=occurred + timedelta(hours=2),
        notes="quick fix",
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    # No reset → MAX (table has rows but none qualify).
    assert days == MAX_REPORTABLE_STREAK_DAYS


@pytest.mark.asyncio
async def test_severe_type_resets_immediately(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """BALANCE_DISCREPANCY resets even if resolved instantly."""
    repo = CriticalIncidentRepository(async_session_factory)
    occurred = _now() - timedelta(days=5)
    pk = await repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=occurred,
        auto_detected=True,
    )
    await repo.resolve_incident(
        pk,
        resolved_at=occurred + timedelta(minutes=10),
        notes="auto",
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    # 5 days since the severe incident.
    assert 4 <= days <= 5


@pytest.mark.asyncio
async def test_orphan_unresolved_severe_resets(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    occurred = _now() - timedelta(days=3)
    await repo.add(
        type_=CriticalIncidentType.RECONCILE_ORPHAN_UNRESOLVED,
        occurred_at=occurred,
        auto_detected=True,
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    assert 2 <= days <= 3


@pytest.mark.asyncio
async def test_long_resolution_resets(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Non-severe but resolution took > 24h: counts as reset at resolved_at."""
    repo = CriticalIncidentRepository(async_session_factory)
    occurred = _now() - timedelta(days=10)
    pk = await repo.add(
        type_=CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL,
        occurred_at=occurred,
        auto_detected=True,
    )
    resolved = occurred + timedelta(hours=48)  # 2 days later
    await repo.resolve_incident(
        pk, resolved_at=resolved, notes="slow recovery"
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    # last_reset_at == resolved (= 8 days ago).
    assert 7 <= days <= 8


@pytest.mark.asyncio
async def test_unresolved_older_than_24h_counts_as_reset(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """An unresolved non-severe incident older than 24h has effectively
    crossed the threshold; reset timestamp is occurred + 24h.
    """
    repo = CriticalIncidentRepository(async_session_factory)
    occurred = _now() - timedelta(days=5)
    await repo.add(
        type_=CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL,
        occurred_at=occurred,
        auto_detected=True,
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    # Reset at occurred + 24h = 4 days ago.
    assert 3 <= days <= 4


@pytest.mark.asyncio
async def test_takes_max_of_multiple_resets(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Several reset events; the streak is days since the LATEST one."""
    repo = CriticalIncidentRepository(async_session_factory)
    # Old severe (10 days ago).
    await repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_now() - timedelta(days=10),
        auto_detected=True,
    )
    # Recent severe (2 days ago).
    await repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_now() - timedelta(days=2),
        auto_detected=True,
    )
    days = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    assert 1 <= days <= 2


# ─── Integration with mode_guards ────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_guards_days_clean_streak_uses_real_query(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """The mode_guards function reads from critical_incidents.

    Inside a running event loop the sync helper falls back to the
    permissive MAX value (documented behaviour) — the *async-aware*
    code path lives in :func:`compute_days_clean_streak` which is
    covered above. We assert the permissive fallback here.
    """
    from mib.trading.mode_guards import days_clean_streak  # noqa: PLC0415

    streak = days_clean_streak()
    assert streak == MAX_REPORTABLE_STREAK_DAYS

    # Even with incidents, the in-loop sync caller still falls back
    # to MAX — operator-facing usage hits this from mode_force /
    # /clean_streak which run inside the loop. The async path
    # produces the real value, exercised in the other tests.
    repo = CriticalIncidentRepository(async_session_factory)
    await repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_now() - timedelta(days=3),
        auto_detected=True,
    )
    real = await compute_days_clean_streak(
        session_factory=async_session_factory
    )
    assert 2 <= real <= 3
