"""News-monitor job — fires every 15 min.

For every ticker present in *any* user's watchlist, pulls the latest
headlines, attaches sentiment via the IA router and pushes bullish /
bearish items (not neutral) to every watcher.

Dedup via ``ProcessedNews(url_hash)`` — a URL is processed at most
once across all watchers. To avoid flooding if several users share a
ticker we still individually track deliveries through ``SentAlert``
with ``alert_type="news:<url_hash[:8]>"``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mib.api.dependencies import get_ai_service, get_news_service
from mib.db.models import ProcessedNews, SentAlert, WatchlistItem
from mib.db.session import async_session_factory
from mib.logger import logger
from mib.telegram.formatters import esc, sentiment_emoji

if TYPE_CHECKING:
    from mib.telegram import BotApp

# Per-user dedup window (independent of the global url-hash dedup).
_DEDUP_WINDOW = timedelta(hours=24)

# Only push bullish / bearish. Neutral is noise for alerts.
_PUSH_SENTIMENTS = {"bullish", "bearish"}


async def run_news_monitor_job(app: BotApp) -> None:
    """Fetch news for every watched ticker, classify, push interesting items."""
    async with async_session_factory() as session:
        by_ticker = await _load_watches(session)
        if not by_ticker:
            return

        ai = get_ai_service()
        news = get_news_service()

        delivered = 0
        for ticker, watchers in by_ticker.items():
            try:
                resp = await news.for_ticker(ticker, limit=3)
            except Exception as exc:  # noqa: BLE001
                logger.info("news_monitor: {} fetch failed: {}", ticker, exc)
                continue

            for item in resp.items:
                url = item.url or ""
                if not url:
                    continue
                url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()

                # First watcher to see it gets the AI sentiment; subsequent
                # ones reuse the stored one. We widen to ``str`` here — the
                # ``Sentiment`` literal is enforced at the AI boundary and
                # the DB can (theoretically) hold anything, so we just check
                # the set below.
                processed = await _get_processed(session, url_hash)
                sentiment: str
                rationale: str
                if processed is None:
                    sent_lit, rationale = await ai.news_sentiment(
                        item.headline, item.summary
                    )
                    sentiment = sent_lit
                    session.add(
                        ProcessedNews(
                            url_hash=url_hash,
                            ticker=ticker,
                            sentiment=sentiment,
                        )
                    )
                    # Flush so a subsequent ticker that duplicates the URL
                    # sees the row in the same loop.
                    await session.flush()
                else:
                    sentiment = processed.sentiment or "neutral"
                    rationale = ""

                if sentiment not in _PUSH_SENTIMENTS:
                    continue

                alert_type = f"news:{url_hash[:12]}"
                for user_id in watchers:
                    if await _was_recently_sent(
                        session, user_id, ticker, alert_type
                    ):
                        continue
                    await _notify(
                        app,
                        user_id,
                        ticker,
                        item.headline,
                        item.source,
                        url,
                        sentiment,
                        rationale,
                    )
                    session.add(
                        SentAlert(
                            user_id=user_id,
                            ticker=ticker,
                            alert_type=alert_type,
                        )
                    )
                    delivered += 1
        await session.commit()
        if delivered:
            logger.info("news_monitor: delivered {} notification(s)", delivered)


async def _load_watches(session: AsyncSession) -> dict[str, list[int]]:
    stmt = select(WatchlistItem.user_id, WatchlistItem.ticker)
    rows = (await session.execute(stmt)).all()
    out: dict[str, list[int]] = defaultdict(list)
    for user_id, ticker in rows:
        out[ticker].append(user_id)
    return out


async def _get_processed(
    session: AsyncSession, url_hash: str
) -> ProcessedNews | None:
    stmt = select(ProcessedNews).where(ProcessedNews.url_hash == url_hash).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


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
    headline: str,
    source: str,
    url: str,
    sentiment: str,
    rationale: str,
) -> None:
    emoji = sentiment_emoji(sentiment)
    body = (
        f"📰 <b>{esc(ticker)}</b>\n"
        f'{emoji} <a href="{esc(url)}">{esc(headline)}</a>\n'
        f"<i>· {esc(source)}</i>"
    )
    if rationale:
        body += f"\n<i>· {esc(rationale)}</i>"
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_monitor: send to {} failed: {}", user_id, exc)
