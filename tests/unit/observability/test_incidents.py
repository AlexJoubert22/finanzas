"""Tests for :class:`CriticalIncidentRepository` (FASE 13.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.session import async_session_factory
from mib.observability.incidents import (
    SEVERE_TYPES_RESET_ALWAYS,
    CriticalIncidentRepository,
    CriticalIncidentType,
    IncidentAlreadyResolvedError,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ─── Enum ────────────────────────────────────────────────────────────


def test_seven_incident_types_exact() -> None:
    """Spec lock-in: ROADMAP Apéndice A specifies exactly 7 types."""
    expected = {
        "reconcile.orphan_unresolved",
        "reconcile.balance_unattributed",
        "circuit_breaker.open_over_15min",
        "executor.stop_missing_post_fill",
        "risk.kill_switch_daily_dd",
        "ops.manual_intervention",
        "reconcile.failed_over_30min",
    }
    actual = {t.value for t in CriticalIncidentType}
    assert actual == expected


def test_severe_types_reset_always_set_includes_correct_pair() -> None:
    """The two always-reset types are documented in 13.5 spec."""
    assert SEVERE_TYPES_RESET_ALWAYS == frozenset({
        CriticalIncidentType.BALANCE_DISCREPANCY,
        CriticalIncidentType.RECONCILE_ORPHAN_UNRESOLVED,
    })


# ─── add() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_persists_full_row(fresh_db: None) -> None:  # noqa: ARG001
    repo = CriticalIncidentRepository(async_session_factory)
    pk = await repo.add(
        type_=CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED,
        occurred_at=_now(),
        auto_detected=False,
        severity="critical",
        context={"reason": "operator stop", "user": "u:1"},
    )
    assert pk > 0
    row = await repo.get_by_id(pk)
    assert row is not None
    assert row.type == CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED
    assert row.severity == "critical"
    assert row.auto_detected is False
    assert row.resolved_at is None
    assert row.resolution_notes is None
    assert row.context == {"reason": "operator stop", "user": "u:1"}


@pytest.mark.asyncio
async def test_add_default_severity_warning(fresh_db: None) -> None:  # noqa: ARG001
    repo = CriticalIncidentRepository(async_session_factory)
    pk = await repo.add(
        type_=CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL,
        occurred_at=_now(),
        auto_detected=True,
    )
    row = await repo.get_by_id(pk)
    assert row is not None
    assert row.severity == "warning"
    assert row.auto_detected is True


# ─── resolve_incident() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_sets_resolved_at_and_notes(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    pk = await repo.add(
        type_=CriticalIncidentType.RECONCILE_ORPHAN_UNRESOLVED,
        occurred_at=_now() - timedelta(hours=2),
        auto_detected=True,
    )
    resolved_at = _now()
    updated = await repo.resolve_incident(
        pk, resolved_at=resolved_at, notes="manually closed by operator"
    )
    assert updated is not None
    assert updated.resolved_at == resolved_at
    assert updated.resolution_notes == "manually closed by operator"


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_none(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    out = await repo.resolve_incident(
        9999, resolved_at=_now(), notes="x"
    )
    assert out is None


@pytest.mark.asyncio
async def test_resolve_twice_raises(fresh_db: None) -> None:  # noqa: ARG001
    """Set-once contract: second resolve raises, the row stays unchanged."""
    repo = CriticalIncidentRepository(async_session_factory)
    pk = await repo.add(
        type_=CriticalIncidentType.BALANCE_DISCREPANCY,
        occurred_at=_now() - timedelta(hours=1),
        auto_detected=True,
    )
    await repo.resolve_incident(pk, resolved_at=_now(), notes="first")
    with pytest.raises(IncidentAlreadyResolvedError) as exc_info:
        await repo.resolve_incident(
            pk, resolved_at=_now(), notes="second"
        )
    assert exc_info.value.incident_id == pk
    # Original notes preserved.
    row = await repo.get_by_id(pk)
    assert row is not None
    assert row.resolution_notes == "first"


# ─── Reads ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_orders_descending(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    base = _now() - timedelta(hours=2)
    await repo.add(
        type_=CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL,
        occurred_at=base,
        auto_detected=True,
    )
    await repo.add(
        type_=CriticalIncidentType.KILL_SWITCH_DD_DAILY,
        occurred_at=base + timedelta(minutes=30),
        auto_detected=True,
    )
    rows = await repo.list_recent(limit=10)
    # Most recent first.
    assert rows[0].type == CriticalIncidentType.KILL_SWITCH_DD_DAILY
    assert rows[1].type == CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL


@pytest.mark.asyncio
async def test_list_recent_filters_by_since(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    old = _now() - timedelta(days=2)
    recent = _now() - timedelta(hours=1)
    await repo.add(
        type_=CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED,
        occurred_at=old,
        auto_detected=False,
    )
    await repo.add(
        type_=CriticalIncidentType.RECONCILE_FAILED_PROLONGED,
        occurred_at=recent,
        auto_detected=True,
    )
    rows = await repo.list_recent(
        since=_now() - timedelta(hours=24)
    )
    assert len(rows) == 1
    assert rows[0].type == CriticalIncidentType.RECONCILE_FAILED_PROLONGED


# ─── DB CHECK constraint ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_type_rejected_at_db_level(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """The CHECK constraint blocks any type outside the 7-value set."""
    from sqlalchemy import text  # noqa: PLC0415

    async with async_session_factory() as session, session.begin():
        with pytest.raises(Exception, match="ck_critical_incidents_type|CHECK"):
            await session.execute(
                text(
                    "INSERT INTO critical_incidents "
                    "(type, occurred_at, auto_detected, severity) "
                    "VALUES ('not.a.real.type', CURRENT_TIMESTAMP, 1, 'warning')"
                )
            )
