"""/status handler — uptime + source health + IA quotas + portfolio.

Pulls the same data the HTTP ``/health`` endpoint emits, avoids an
HTTP round-trip by invoking the dependency getters directly. FASE 8.2
adds a portfolio block: equity, open position count, last sync age.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_router, get_portfolio_state
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

    portfolio_block: dict[str, object] | None = None
    try:
        snapshot = await get_portfolio_state().snapshot()
        age = (datetime.now(UTC) - snapshot.last_synced_at).total_seconds()
        portfolio_block = {
            "equity_quote": str(snapshot.equity_quote),
            "open_positions": len(snapshot.positions),
            "last_synced_age_seconds": age,
            "source": snapshot.source,
        }
    except Exception as exc:  # noqa: BLE001
        logger.info("/status portfolio snapshot failed: {}", exc)

    if not sources_status:
        overall = "starting"
    elif any(v in {"down", "degraded"} for v in sources_status.values()):
        overall = "degraded"
    else:
        overall = "ok"

    payload: dict[str, object] = {
        "status": overall,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT),
        "sources_status": sources_status,
        "ai_quotas": ai_quotas,
    }
    if portfolio_block is not None:
        payload["portfolio"] = portfolio_block
    await update.message.reply_html(fmt_status(payload))
