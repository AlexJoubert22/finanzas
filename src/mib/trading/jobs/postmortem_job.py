"""Scheduler job: nightly postmortem at 02:00 UTC (FASE 11.4)."""

from __future__ import annotations

from mib.api.dependencies import get_postmortem_runner
from mib.logger import logger
from mib.trading.postmortem import yesterday_utc_date


async def postmortem_job() -> None:
    """One nightly tick: postmortem of trades closed in the last 24h.

    The cron fires at 02:00 UTC, but the analysis target is the
    *previous* UTC day so a trade closed at 23:59 still lands in the
    right batch. Idempotent — re-runs for the same date return the
    existing row.
    """
    runner = get_postmortem_runner()
    target = yesterday_utc_date()
    try:
        report = await runner.run_for_date(target)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "postmortem_job: unexpected failure for {}: {}", target, exc
        )
        return
    logger.info(
        "postmortem_job: date={} trades_analyzed={} success={} row_id={}",
        report.date_utc,
        report.trades_analyzed,
        report.success,
        report.row_id,
    )
