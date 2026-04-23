"""/status handler — uptime + source health + IA quotas.

Pulls the same data the HTTP ``/health`` endpoint emits, avoids an
HTTP round-trip by invoking the dependency getters directly.
"""

from __future__ import annotations

import time

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_router
from mib.logger import logger
from mib.services.health_probe import get_health_cache
from mib.telegram.formatters import fmt_status

# Monotonic marker — stamped on first import so /status shows bot uptime.
_STARTED_AT = time.monotonic()


async def status(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    sources_status = get_health_cache().snapshot()
    try:
        ai_quotas = await get_ai_router().usage_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.info("/status ai quota snapshot failed: {}", exc)
        ai_quotas = {}

    if not sources_status:
        overall = "starting"
    elif any(v in {"down", "degraded"} for v in sources_status.values()):
        overall = "degraded"
    else:
        overall = "ok"

    payload = {
        "status": overall,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT),
        "sources_status": sources_status,
        "ai_quotas": ai_quotas,
    }
    await update.message.reply_html(fmt_status(payload))
