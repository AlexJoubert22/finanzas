"""``GET /heartbeat`` — public dead-man endpoint (FASE 13.7).

Designed for an external monitor (GitHub Actions cron via Cloudflare
Tunnel — see :doc:`docs/DEAD-MAN-SETUP.md`) to ping every 5 minutes.
On 503 the operator gets paged.

Liveness checks:

- ``last_tick_at``: scheduler ticked recently (every 30s in steady
  state). Threshold defaults to 60s.
- ``last_reconcile_at``: last successful reconcile pass. Threshold
  defaults to 600s (10min).

Auth: ``?token=<HEARTBEAT_TOKEN>`` query param. When the setting is
empty the endpoint refuses every request (operator hasn't enabled
the dead-man yet). The token compare uses
:func:`secrets.compare_digest` for timing-safe equality.

Output: minimalist JSON; no internal state leakage. ``status`` is
``ok`` / ``stalled``; the failing component (when stalled) is named
in ``reason`` so the operator's pager knows where to look.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from mib.config import get_settings
from mib.observability.scheduler_health import get_scheduler_health

router = APIRouter(tags=["observability"])


@router.get("/heartbeat", include_in_schema=False)
async def heartbeat(token: str = Query(default="")) -> JSONResponse:
    settings = get_settings()
    if not settings.heartbeat_token:
        # No token configured -> dead-man explicitly disabled.
        raise HTTPException(status_code=503, detail="dead-man disabled")
    if not secrets.compare_digest(token, settings.heartbeat_token):
        raise HTTPException(status_code=401, detail="bad token")

    health = get_scheduler_health()
    now = datetime.now(UTC).replace(tzinfo=None)

    if health.last_tick_at is None:
        return _stalled(reason="scheduler never ticked", now=now)
    age_tick = (now - health.last_tick_at).total_seconds()
    if age_tick > settings.heartbeat_scheduler_max_age_sec:
        return _stalled(
            reason=f"scheduler stalled ({int(age_tick)}s since last tick)",
            now=now,
        )

    if health.last_reconcile_at is None:
        return _stalled(reason="reconciler never ran", now=now)
    age_recon = (now - health.last_reconcile_at).total_seconds()
    if age_recon > settings.heartbeat_reconcile_max_age_sec:
        return _stalled(
            reason=(
                f"reconcile stalled ({int(age_recon)}s since last "
                "successful run)"
            ),
            now=now,
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "ts": now.isoformat(),
        },
    )


def _stalled(*, reason: str, now: datetime) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "status": "stalled",
            "reason": reason,
            "ts": now.isoformat(),
        },
    )
