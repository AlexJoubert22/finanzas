"""Scheduler job: run the reconciler every 5 minutes (FASE 9.5).

Mirrors :mod:`mib.trading.jobs.portfolio_sync` — never raises, swallows
all errors, leaves the previous report's findings in the DB so the
``/reconcile`` command can still page through history if the latest
run failed.
"""

from __future__ import annotations

from mib.api.dependencies import get_reconciler
from mib.logger import logger


async def reconcile_job() -> None:
    """One tick of the reconciliation loop, triggered by APScheduler."""
    reconciler = get_reconciler()
    try:
        report = await reconciler.reconcile(triggered_by="scheduler")
    except Exception as exc:  # noqa: BLE001 — never crash the scheduler
        logger.warning("reconcile_job: unexpected failure: {}", exc)
        return
    logger.info(
        "reconcile_job: status={} discrepancies={} run_id={}",
        report.status,
        len(report.discrepancies),
        report.run_id,
    )
