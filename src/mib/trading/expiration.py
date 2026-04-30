"""TTL expiration job for pending signals.

Scheduled every 15 minutes. Reads pending signals whose ``expires_at``
is already in the past and transitions them to ``expired`` via the
repository's append-only :meth:`SignalRepository.transition` helper.

The job is **idempotent**: re-running on a freshly-expired set is a
no-op because subsequent reads see ``status='expired'`` and the
candidate query filters those out. Concurrent execution is safe by
construction — APScheduler is configured with ``max_instances=1`` so
two ticks cannot overlap, and even if they did, the per-row transition
serializes through SQLite WAL.

# TODO FASE 28 — gate "stale by price". Beyond temporal TTL, a
# signal whose entry_zone is no longer reachable (price moved more
# than X% past the zone) is also functionally dead. Implementing it
# here would require fetching live quotes for every pending signal
# each tick, multiplying exchange calls. Defer to FASE 28 alongside
# the calendar-awareness work, which already needs market data per
# evaluation. Reaffirmed in FASE 8.7 strategic review 2026-04-28.
"""

from __future__ import annotations

from datetime import datetime

from mib.api.dependencies import get_signal_repository
from mib.logger import logger
from mib.trading.signal_repo import StaleSignalStateError


async def expire_stale_signals_job(*, now: datetime | None = None) -> int:
    """Transition every pending signal past ``expires_at`` to ``expired``.

    Returns the number of signals successfully transitioned. Errors on
    individual signals are logged and skipped — the job never raises,
    so APScheduler does not retry the whole batch on partial failure.
    """
    repo = get_signal_repository()
    candidates = await repo.list_expired_pending(now=now)

    if not candidates:
        logger.debug("expire_stale_signals: no candidates this tick")
        return 0

    expired_count = 0
    for candidate in candidates:
        signal_id = candidate.id
        try:
            result = await repo.transition(
                signal_id,
                "expired",
                actor="job:expire_stale_signals",
                event_type="expired",
                reason=f"TTL elapsed at {datetime.now().astimezone().isoformat()}",
                # Race protection: another actor (Telegram callback)
                # may have transitioned this signal between our list
                # query and this update. Skip those gracefully.
                expected_from_status="pending",
            )
            if result is not None:
                expired_count += 1
        except StaleSignalStateError as exc:
            logger.info(
                "expire_stale_signals: skip {} (race: {} -> {})",
                signal_id,
                exc.expected,
                exc.actual,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.warning(
                "expire_stale_signals: failed on {}: {}", signal_id, exc
            )

    logger.info(
        "expire_stale_signals: expired {}/{} candidates",
        expired_count,
        len(candidates),
    )
    return expired_count
