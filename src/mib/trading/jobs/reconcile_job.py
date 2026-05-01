"""Scheduler job: run the reconciler every 5 minutes (FASE 9.5).

FASE 13.3: also feeds the
:class:`mib.observability.reconcile_supervisor.ReconcileFailureSupervisor`
which emits ``RECONCILE_FAILED_PROLONGED`` after 30 minutes of
consecutive failures.

Mirrors :mod:`mib.trading.jobs.portfolio_sync` — never raises, swallows
all errors, leaves the previous report's findings in the DB so the
``/reconcile`` command can still page through history if the latest
run failed.
"""

from __future__ import annotations

from mib.api.dependencies import get_incident_emitter, get_reconciler
from mib.logger import logger
from mib.observability.reconcile_supervisor import get_reconcile_supervisor


async def reconcile_job() -> None:
    """One tick of the reconciliation loop, triggered by APScheduler."""
    reconciler = get_reconciler()
    supervisor = get_reconcile_supervisor()
    success: bool
    try:
        report = await reconciler.reconcile(triggered_by="scheduler")
        success = report.status != "error"
    except Exception as exc:  # noqa: BLE001 — never crash the scheduler
        logger.warning("reconcile_job: unexpected failure: {}", exc)
        success = False
        report = None  # type: ignore[assignment]

    # Feed the supervisor regardless of outcome.
    try:
        await supervisor.record_run(
            success=success,
            emitter=get_incident_emitter(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile_job: supervisor record_run failed: {}", exc)

    if report is not None:
        logger.info(
            "reconcile_job: status={} discrepancies={} run_id={}",
            report.status,
            len(report.discrepancies),
            report.run_id,
        )
