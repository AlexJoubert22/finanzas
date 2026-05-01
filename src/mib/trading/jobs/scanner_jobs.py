"""Scheduled scanner jobs (PAPER prep).

Reads ``config/scanner_universe.yaml`` and registers one APScheduler
job per preset, each with the cron declared in the YAML. The actual
work delegates to :func:`scanner_to_signals_job` from
:mod:`mib.trading.notify`, which is also what the ad-hoc ``/scan``
Telegram handler invokes — so a scheduled scan and a manual one
follow exactly the same code path.

Boot semantics:

- A malformed YAML raises at boot; the operator sees the problem
  before the scheduler kicks off any job.
- An absent bot or empty ``operator_telegram_id`` is **not** a
  failure: jobs still register but skip with an INFO log when fired.
  This keeps the API-only deployment shape working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.triggers.cron import CronTrigger

from mib.config import get_settings
from mib.logger import logger
from mib.observability.scheduler_health import get_scheduler_health
from mib.services.scanner import PresetName
from mib.trading.scanner_universe import (
    ScannerUniverse,
    default_universe_path,
)

if TYPE_CHECKING:  # pragma: no cover
    from apscheduler.schedulers.asyncio import AsyncIOScheduler


_PAPER_UNIVERSE: tuple[str, ...] = ()


def get_paper_universe() -> tuple[str, ...]:
    """Last universe loaded by :func:`register_scanner_jobs`. Empty
    tuple before registration. Useful for /paper_status read-out.
    """
    return _PAPER_UNIVERSE


def register_scanner_jobs(sched: AsyncIOScheduler) -> ScannerUniverse:
    """Register one scheduled job per preset declared in the YAML.

    Returns the parsed :class:`ScannerUniverse` so the caller can
    surface counts in startup logs.
    """
    universe = ScannerUniverse.from_yaml(default_universe_path())

    global _PAPER_UNIVERSE  # noqa: PLW0603 — module-level cache
    _PAPER_UNIVERSE = universe.paper_universe

    for preset, schedule in universe.schedules.items():
        sched.add_job(
            _make_runner(preset, list(universe.paper_universe)),
            trigger=CronTrigger.from_crontab(schedule.cron, timezone="UTC"),
            id=f"scanner_{preset}",
            name=f"Scheduled scanner ({preset}, {schedule.timeframe})",
            replace_existing=True,
        )
    logger.info(
        "scheduler: registered scanner jobs for {} preset(s) over {} ticker(s)",
        len(universe.schedules),
        len(universe.paper_universe),
    )
    return universe


def _make_runner(preset: PresetName, tickers: list[str]):  # type: ignore[no-untyped-def]
    """Build a no-arg coroutine APScheduler can call. We bind ``preset``
    and ``tickers`` here so the scheduler ID stays a string and the
    job has its parameters captured at registration.
    """

    async def _run() -> None:
        get_scheduler_health().mark_tick()
        settings = get_settings()
        if not settings.telegram_bot_token or settings.operator_telegram_id == 0:
            logger.info(
                "scanner_jobs[{}]: bot token or operator_telegram_id missing "
                "— skip", preset,
            )
            return

        # Late imports keep this module importable in API-only mode
        # (no Telegram client constructed).
        from mib.telegram.bot import get_bot_app  # noqa: PLC0415
        from mib.trading.notify import scanner_to_signals_job  # noqa: PLC0415

        bot_app = get_bot_app()
        if bot_app is None:
            logger.info("scanner_jobs[{}]: bot not running — skip", preset)
            return
        try:
            count = await scanner_to_signals_job(
                bot_app,
                preset=preset,
                tickers=tickers,
                notify_chat_id=settings.operator_telegram_id,
            )
            logger.info(
                "scanner_jobs[{}]: produced {} signals over {} tickers",
                preset, count, len(tickers),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scanner_jobs[{}]: run failed: {}", preset, exc,
            )

    return _run
