"""Tests for IncidentEmitter + circuit_breaker + reconcile_supervisor (FASE 13.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.session import async_session_factory
from mib.observability.circuit_breaker import (
    PROLONGED_OPEN_THRESHOLD,
    CircuitBreakerRegistry,
)
from mib.observability.emitter import IncidentEmitter
from mib.observability.incidents import (
    CriticalIncidentRepository,
    CriticalIncidentType,
)
from mib.observability.metrics import _reset_for_tests as _reset_metrics
from mib.observability.metrics import (
    get_metrics_registry,
    render_metrics_text,
)
from mib.observability.reconcile_supervisor import (
    PROLONGED_FAILURE_THRESHOLD,
    ReconcileFailureSupervisor,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    _reset_metrics()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ─── IncidentEmitter ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emitter_writes_db_and_bumps_metric(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    pk = await emitter.emit(
        type_=CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED,
        context={"reason": "test"},
        severity="warning",
        auto_detected=False,
    )
    # DB row.
    row = await repo.get_by_id(pk)
    assert row is not None
    assert row.type == CriticalIncidentType.MANUAL_INTERVENTION_REQUIRED
    # Metric.
    text = render_metrics_text().decode()
    assert (
        'mib_critical_incident_total{type="ops.manual_intervention"} 1.0'
    ) in text


@pytest.mark.asyncio
async def test_emitter_metric_failure_does_not_block_db(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Prometheus bump raises, the DB row still lands."""
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)

    def _bad_registry():  # type: ignore[no-untyped-def]
        raise RuntimeError("registry blew up")

    monkeypatch.setattr(
        "mib.observability.emitter.get_metrics_registry", _bad_registry
    )
    pk = await emitter.emit(
        type_=CriticalIncidentType.RECONCILE_FAILED_PROLONGED,
        auto_detected=True,
    )
    row = await repo.get_by_id(pk)
    assert row is not None


# ─── CircuitBreakerRegistry ──────────────────────────────────────────


def test_circuit_breaker_open_close_cycle() -> None:
    reg = CircuitBreakerRegistry()
    assert reg.is_open("binance") is False
    reg.open("binance")
    assert reg.is_open("binance") is True
    reg.close("binance")
    assert reg.is_open("binance") is False


@pytest.mark.asyncio
async def test_circuit_breaker_emits_when_prolonged(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    reg = CircuitBreakerRegistry()
    reg.open("binance_orders")
    # Simulate 'opened 20 minutes ago'.
    reg.breakers["binance_orders"].opened_at = (
        _now() - timedelta(minutes=20)
    )
    n = await reg.check_prolonged(emitter)
    assert n == 1
    rows = await repo.list_recent()
    assert len(rows) == 1
    assert rows[0].type == CriticalIncidentType.CIRCUIT_BREAKER_PROLONGED
    assert rows[0].severity == "critical"
    # Second call without further drift should NOT re-emit (idempotent
    # per opened_at).
    n2 = await reg.check_prolonged(emitter)
    assert n2 == 0


@pytest.mark.asyncio
async def test_circuit_breaker_below_threshold_no_emit(
    fresh_db: None,  # noqa: ARG001
) -> None:
    reg = CircuitBreakerRegistry()
    reg.open("binance")
    # Just opened — far below 15-min threshold.
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    n = await reg.check_prolonged(emitter)
    assert n == 0
    rows = await repo.list_recent()
    assert rows == []


def test_circuit_breaker_threshold_constant_15min() -> None:
    """Spec lock-in: 15 min threshold."""
    assert PROLONGED_OPEN_THRESHOLD == timedelta(minutes=15)


# ─── ReconcileFailureSupervisor ──────────────────────────────────────


def test_reconcile_supervisor_threshold_constant_30min() -> None:
    """Spec lock-in: 30 min threshold for prolonged reconcile failure."""
    assert PROLONGED_FAILURE_THRESHOLD == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_reconcile_supervisor_first_failure_no_emit(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """First failed run starts the streak; no incident emitted yet."""
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    sup = ReconcileFailureSupervisor()
    fired = await sup.record_run(success=False, emitter=emitter)
    assert fired is False
    assert sup.streak_started_at is not None
    assert sup.emitted_for_streak is False


@pytest.mark.asyncio
async def test_reconcile_supervisor_emits_after_30min_streak(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    sup = ReconcileFailureSupervisor()
    base = _now() - timedelta(minutes=35)
    await sup.record_run(success=False, emitter=emitter, now=base)
    fired = await sup.record_run(success=False, emitter=emitter, now=_now())
    assert fired is True
    assert sup.emitted_for_streak is True
    rows = await repo.list_recent()
    assert len(rows) == 1
    assert rows[0].type == CriticalIncidentType.RECONCILE_FAILED_PROLONGED


@pytest.mark.asyncio
async def test_reconcile_supervisor_success_resets_streak(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    sup = ReconcileFailureSupervisor()
    base = _now() - timedelta(minutes=35)
    await sup.record_run(success=False, emitter=emitter, now=base)
    await sup.record_run(success=False, emitter=emitter, now=_now())
    # A successful run wipes the streak.
    await sup.record_run(success=True, emitter=emitter)
    assert sup.streak_started_at is None
    assert sup.emitted_for_streak is False
    # Subsequent failure starts fresh; no immediate emit.
    fired = await sup.record_run(success=False, emitter=emitter)
    assert fired is False


@pytest.mark.asyncio
async def test_reconcile_supervisor_one_shot_per_streak(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Within the same streak, the supervisor doesn't re-emit."""
    repo = CriticalIncidentRepository(async_session_factory)
    emitter = IncidentEmitter(repo)
    sup = ReconcileFailureSupervisor()
    base = _now() - timedelta(minutes=35)
    await sup.record_run(success=False, emitter=emitter, now=base)
    fired_first = await sup.record_run(
        success=False, emitter=emitter, now=_now()
    )
    fired_second = await sup.record_run(
        success=False, emitter=emitter, now=_now()
    )
    assert fired_first is True
    assert fired_second is False
    rows = await repo.list_recent()
    assert len(rows) == 1


# Suppress unused-import warning for shared fixture.
_ = get_metrics_registry
