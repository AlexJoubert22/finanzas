"""Price-alerts job — fires every 60 s.

For each active ``PriceAlert`` row, fetches the current price and, if
the operator condition is satisfied, sends a Telegram notification and
marks the alert inactive + logs the trigger in ``sent_alerts``.

FASE 5 mitigations applied:
    - APScheduler ``max_instances=1`` + ``coalesce=True`` (set in scheduler.py).
    - Prices are fetched once per *unique ticker* via ``asyncio.gather``
      — not once per alert — so 50 alerts on BTC issue 1 HTTP call.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mib.api.dependencies import get_market_service
from mib.db.models import PriceAlert, SentAlert
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.telegram.formatters import fmt_watch_triggered

if TYPE_CHECKING:
    from mib.telegram import BotApp


async def run_price_alerts_job(app: BotApp) -> None:
    """Scan active alerts and fire those whose condition is met."""
    async with async_session_factory() as session:
        alerts = await _load_active_alerts(session)
        if not alerts:
            return

        # Group alerts by ticker so we can fetch each price once.
        by_ticker: dict[str, list[PriceAlert]] = defaultdict(list)
        for a in alerts:
            by_ticker[a.ticker].append(a)

        prices = await _fetch_prices(list(by_ticker.keys()))

        triggered = 0
        for ticker, group in by_ticker.items():
            price = prices.get(ticker)
            if price is None:
                continue
            for alert in group:
                if _condition_met(alert, price):
                    await _fire(app, session, alert, price)
                    triggered += 1
        await session.commit()
        if triggered:
            logger.info("price_alerts: fired {} alert(s)", triggered)


async def _load_active_alerts(session: AsyncSession) -> list[PriceAlert]:
    stmt = select(PriceAlert).where(PriceAlert.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Return ``{ticker: last_price}`` — failures are dropped silently."""
    market = get_market_service()

    async def one(t: str) -> tuple[str, float | None]:
        try:
            resp = await market.get_symbol(t, ohlcv_timeframe="1h", ohlcv_limit=1)
            return t, resp.quote.price
        except Exception as exc:  # noqa: BLE001
            logger.info("price_alerts: fetch {} failed: {}", t, exc)
            return t, None

    results = await asyncio.gather(*(one(t) for t in tickers))
    return {t: p for t, p in results if p is not None}


def _condition_met(alert: PriceAlert, price: float) -> bool:
    if alert.operator == ">":
        return price > alert.target_price
    if alert.operator == "<":
        return price < alert.target_price
    return False


async def _fire(
    app: BotApp,
    session: AsyncSession,
    alert: PriceAlert,
    price: float,
) -> None:
    """Send notification + mark inactive + log to sent_alerts."""
    body = fmt_watch_triggered(alert.ticker, alert.operator, alert.target_price, price)
    try:
        await app.bot.send_message(
            chat_id=alert.user_id,
            text=body,
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001 - never crash the scheduler
        logger.warning(
            "price_alerts: send_message to {} failed: {}", alert.user_id, exc
        )
        return  # don't deactivate — retry next tick

    alert.is_active = False
    from datetime import UTC, datetime

    alert.triggered_at = datetime.now(UTC)
    session.add(
        SentAlert(
            user_id=alert.user_id,
            ticker=alert.ticker,
            alert_type="price",
        )
    )
