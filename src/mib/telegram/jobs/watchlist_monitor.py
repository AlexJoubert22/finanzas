"""Watchlist-monitor job — fires every 5 min.

For every ticker in every user's watchlist, pulls the 1h quote +
indicators and flags *anomalies*:

    - RSI < 30 → oversold
    - RSI > 70 → overbought
    - |24h change| > 5% → significant move

Dedup rule: a given (user, ticker, alert_type) pair only fires once per
24 h. We store the stamp in ``sent_alerts``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mib.api.dependencies import get_market_service
from mib.db.models import SentAlert, WatchlistItem
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.telegram.formatters import esc, fmt_pct, fmt_price

if TYPE_CHECKING:
    from mib.telegram import BotApp

# Dedup window per (user, ticker, alert_type).
_DEDUP_WINDOW = timedelta(hours=24)


async def run_watchlist_monitor_job(app: BotApp) -> None:
    """Walk all watchlists, detect anomalies, notify once per 24h each."""
    async with async_session_factory() as session:
        by_ticker = await _load_watches(session)
        if not by_ticker:
            return

        snapshots = await _fetch_snapshots(list(by_ticker.keys()))

        notified = 0
        for ticker, watchers in by_ticker.items():
            snap = snapshots.get(ticker)
            if snap is None:
                continue
            anomalies = _detect_anomalies(snap)
            if not anomalies:
                continue
            for user_id in watchers:
                for atype, msg in anomalies:
                    if await _was_recently_sent(session, user_id, ticker, atype):
                        continue
                    await _notify(app, user_id, ticker, atype, msg)
                    session.add(
                        SentAlert(user_id=user_id, ticker=ticker, alert_type=atype)
                    )
                    notified += 1
        await session.commit()
        if notified:
            logger.info("watchlist_monitor: sent {} notification(s)", notified)


async def _load_watches(session: AsyncSession) -> dict[str, list[int]]:
    """Return ``{ticker: [user_id, ...]}`` from ``watchlist_items``."""
    stmt = select(WatchlistItem.user_id, WatchlistItem.ticker)
    rows = (await session.execute(stmt)).all()
    out: dict[str, list[int]] = defaultdict(list)
    for user_id, ticker in rows:
        out[ticker].append(user_id)
    return out


async def _fetch_snapshots(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    """Fetch price + RSI per ticker, one call per unique ticker."""
    market = get_market_service()

    async def one(t: str) -> tuple[str, dict[str, float | None] | None]:
        try:
            resp = await market.get_symbol(t, ohlcv_timeframe="1h", ohlcv_limit=250)
            ind = resp.indicators
            return t, {
                "price": resp.quote.price,
                "change_24h_pct": resp.quote.change_24h_pct,
                "rsi_14": ind.rsi_14 if ind else None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("watchlist_monitor: fetch {} failed: {}", t, exc)
            return t, None

    results = await asyncio.gather(*(one(t) for t in tickers))
    return {t: d for t, d in results if d is not None}


def _detect_anomalies(
    snap: dict[str, float | None],
) -> list[tuple[str, str]]:
    """Return list of ``(alert_type, human_message)`` tuples."""
    out: list[tuple[str, str]] = []
    rsi = snap.get("rsi_14")
    change = snap.get("change_24h_pct")
    price = snap.get("price")

    if rsi is not None:
        if rsi < 30:
            out.append(("rsi_oversold", f"RSI(14) = {rsi:.1f} → sobreventa"))
        elif rsi > 70:
            out.append(("rsi_overbought", f"RSI(14) = {rsi:.1f} → sobrecompra"))

    if change is not None and abs(change) >= 5.0:
        direction = "subió" if change > 0 else "cayó"
        out.append((
            "big_move",
            f"{direction} {fmt_pct(change)} en 24h (precio {fmt_price(price)})",
        ))
    return out


async def _was_recently_sent(
    session: AsyncSession,
    user_id: int,
    ticker: str,
    alert_type: str,
) -> bool:
    cutoff = datetime.now(UTC) - _DEDUP_WINDOW
    stmt = (
        select(SentAlert.id)
        .where(
            SentAlert.user_id == user_id,
            SentAlert.ticker == ticker,
            SentAlert.alert_type == alert_type,
            SentAlert.sent_at >= cutoff,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _notify(
    app: BotApp,
    user_id: int,
    ticker: str,
    alert_type: str,  # noqa: ARG001 - included for future extension / logs
    msg: str,
) -> None:
    text = (
        f"👁 <b>Watchlist · {esc(ticker)}</b>\n"
        f"{esc(msg)}"
    )
    try:
        await app.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "watchlist_monitor: send_message to {} failed: {}", user_id, exc
        )
