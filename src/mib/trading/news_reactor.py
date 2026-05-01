"""News Reactor (FASE 11.3) — proposes reduce/close/hold on open positions.

Pipeline (every 5 min):
1. Read all currently-open trades (status='open' OR 'pending') from
   :class:`TradeRepository.list_open`.
2. For each open trade, fetch the most recent news on its ticker via
   :class:`NewsService.for_ticker`.
3. Filter to "strong-sentiment" items (``abs(sentiment) > 0.7``).
4. Dedupe: skip any (news_url_hash, ticker) pair already proposed in
   the last 30 min by querying ``news_reactions``.
5. Ask the LLM (TaskType.FAST_CLASSIFY) for a one-shot decision:
   ``reduce | close | hold`` with one-sentence justification.
6. Persist the proposal + ship a Telegram informational alert to the
   admin. **Never** executes the action — that's the operator's call.

Idempotency by construction: the dedupe table-read keeps re-running
the job safe. The 30-min window is configurable via
:data:`DEDUPE_WINDOW`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.ai.models import TaskType
from mib.ai.prompts import SYSTEM_NEWS_REACTION_V1
from mib.ai.providers.base import AITask
from mib.ai.router import AIRouter
from mib.db.models import NewsReactionRow
from mib.logger import logger
from mib.models.news import NewsItem
from mib.services.news import NewsService
from mib.trading.alerter import NullAlerter, TelegramAlerter
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import Trade

DEDUPE_WINDOW: timedelta = timedelta(minutes=30)
SENTIMENT_THRESHOLD: float = 0.7
DEFAULT_NEWS_LIMIT: int = 5

NewsDecision = Literal["reduce", "close", "hold"]


@dataclass(frozen=True)
class ReactionProposal:
    """One proposal produced by the reactor for one (news, ticker) pair."""

    ticker: str
    news_headline: str
    news_url_hash: str
    decision: NewsDecision
    justification: str
    provider_used: str
    model_used: str
    latency_ms: int
    position_trade_id: int | None
    decided_at: datetime
    news_sentiment: float | None = None


class NewsReactor:
    """Coordinator for the news reaction proposal pipeline."""

    def __init__(
        self,
        *,
        ai_router: AIRouter,
        news_service: NewsService,
        trade_repo: TradeRepository,
        session_factory: async_sessionmaker[AsyncSession],
        alerter: TelegramAlerter | None = None,
        sentiment_threshold: float = SENTIMENT_THRESHOLD,
        dedupe_window: timedelta = DEDUPE_WINDOW,
    ) -> None:
        self._router = ai_router
        self._news = news_service
        self._trades = trade_repo
        self._sf = session_factory
        self._alerter = alerter or NullAlerter()
        self._sentiment_threshold = sentiment_threshold
        self._dedupe_window = dedupe_window

    async def run_once(self) -> list[ReactionProposal]:
        """One pass. Returns the list of proposals produced this run."""
        try:
            open_trades = await self._trades.list_open()
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.warning("news_reactor: list_open failed: {}", exc)
            return []
        if not open_trades:
            logger.debug("news_reactor: no open trades — skip")
            return []

        proposals: list[ReactionProposal] = []
        for trade in open_trades:
            try:
                trade_proposals = await self._react_for_trade(trade)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "news_reactor: ticker={} crashed: {}", trade.ticker, exc
                )
                continue
            proposals.extend(trade_proposals)

        logger.info(
            "news_reactor: trades_scanned={} proposals_emitted={}",
            len(open_trades),
            len(proposals),
        )
        return proposals

    async def _react_for_trade(
        self, trade: Trade
    ) -> list[ReactionProposal]:
        """Score the news items for one open trade; emit proposals."""
        try:
            response = await self._news.for_ticker(
                trade.ticker, limit=DEFAULT_NEWS_LIMIT
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "news_reactor: news fetch failed for {}: {}",
                trade.ticker,
                exc,
            )
            return []

        proposals: list[ReactionProposal] = []
        for item in response.items:
            if not _is_strong_sentiment(item, self._sentiment_threshold):
                continue
            url_hash = _hash_news(item)
            if await self._is_recent_duplicate(url_hash, trade.ticker):
                continue
            proposal = await self._propose(trade, item, url_hash)
            if proposal is None:
                continue
            await self._persist_and_alert(proposal)
            proposals.append(proposal)
        return proposals

    async def _is_recent_duplicate(
        self, url_hash: str, ticker: str
    ) -> bool:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - self._dedupe_window
        async with self._sf() as session:
            stmt = select(NewsReactionRow.id).where(
                NewsReactionRow.news_url_hash == url_hash,
                NewsReactionRow.ticker == ticker,
                NewsReactionRow.decided_at >= cutoff,
            )
            row = (await session.scalars(stmt)).first()
            return row is not None

    async def _propose(
        self, trade: Trade, item: NewsItem, url_hash: str
    ) -> ReactionProposal | None:
        """Ask the LLM for one decision. Returns None on parse / router failure."""
        user_message = _build_user_message(trade, item)
        task = AITask(
            task_type=TaskType.FAST_CLASSIFY,
            system=SYSTEM_NEWS_REACTION_V1,
            prompt=user_message,
            temperature=0.0,
            max_tokens=200,
        )
        t0 = time.monotonic()
        response = await self._router.complete(task)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if not response.success:
            logger.info(
                "news_reactor: router failed for {}: {}",
                trade.ticker,
                response.error,
            )
            return None

        parsed = _parse_decision_payload(response.content)
        if parsed is None:
            logger.info(
                "news_reactor: parse failed for {}: {!r}",
                trade.ticker,
                response.content[:160],
            )
            return None

        decision, justification = parsed
        return ReactionProposal(
            ticker=trade.ticker,
            news_headline=item.headline,
            news_url_hash=url_hash,
            decision=decision,
            justification=justification[:400],
            provider_used=(
                response.provider.value if response.provider else ""
            ),
            model_used=response.model,
            latency_ms=response.latency_ms or latency_ms,
            position_trade_id=trade.trade_id,
            decided_at=datetime.now(UTC).replace(tzinfo=None),
            news_sentiment=_extract_sentiment(item),
        )

    async def _persist_and_alert(self, proposal: ReactionProposal) -> None:
        async with self._sf() as session, session.begin():
            row = NewsReactionRow(
                news_url_hash=proposal.news_url_hash,
                news_headline=proposal.news_headline[:512],
                news_sentiment=proposal.news_sentiment,
                ticker=proposal.ticker,
                position_trade_id=proposal.position_trade_id,
                decision=proposal.decision,
                justification=proposal.justification,
                ai_provider_used=proposal.provider_used or None,
                ai_model_used=proposal.model_used or None,
                decided_at=proposal.decided_at,
            )
            session.add(row)
        try:
            await self._alerter.alert(
                "📰 <b>News reaction proposal</b>\n"
                f"  ticker: <code>{proposal.ticker}</code>  "
                f"trade #{proposal.position_trade_id}\n"
                f"  decision: <code>{proposal.decision}</code>\n"
                f"  reason: {proposal.justification[:200]}\n"
                f"  source headline: <i>{proposal.news_headline[:140]}</i>\n"
                "<i>Proposal only — no auto-execution. Operator must "
                "act if needed.</i>"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("news_reactor: alert failed: {}", exc)


# ─── Pure helpers ───────────────────────────────────────────────────


def _is_strong_sentiment(item: NewsItem, threshold: float) -> bool:
    """True when ``item.sentiment`` exists and ``|sentiment| > threshold``.

    Sentiment is sourced upstream via FASE 5+ classification; some
    items lack it (RSS without scoring). Those are skipped silently
    to avoid spamming the operator with neutral noise.
    """
    sentiment = _extract_sentiment(item)
    if sentiment is None:
        return False
    return abs(sentiment) > threshold


def _extract_sentiment(item: NewsItem) -> float | None:
    """Return a numeric sentiment if the NewsItem carries one.

    The current ``NewsItem`` schema doesn't expose sentiment as a top-
    level field; some upstream calls populate ``metadata['sentiment']``
    or a string field. We look in known places defensively so adding
    sentiment scoring later doesn't require touching the reactor.
    """
    raw = getattr(item, "sentiment", None)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        # 'bullish' / 'bearish' / 'neutral' coercion to numeric.
        return {
            "bullish": 0.9, "bearish": -0.9, "neutral": 0.0
        }.get(raw.lower())
    metadata = getattr(item, "metadata", None) or {}
    if isinstance(metadata, dict) and "sentiment" in metadata:
        try:
            return float(metadata["sentiment"])
        except (TypeError, ValueError):
            return None
    return None


def _hash_news(item: NewsItem) -> str:
    """Stable hash for dedupe. URL preferred; fallback to headline."""
    seed = (getattr(item, "url", None) or item.headline or "").strip()
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:64]


def _build_user_message(trade: Trade, item: NewsItem) -> str:
    sentiment = _extract_sentiment(item)
    sent_str = (
        f"{sentiment:+.2f}" if sentiment is not None else "(unscored)"
    )
    return (
        "NEWS:\n"
        f"  headline: {item.headline}\n"
        f"  sentiment: {sent_str}\n"
        f"  source: {getattr(item, 'source', 'unknown')}\n"
        f"  url: {getattr(item, 'url', '(none)')}\n\n"
        "OPEN POSITION:\n"
        f"  ticker: {trade.ticker}\n"
        f"  side: {trade.side}\n"
        f"  size: {trade.size}\n"
        f"  entry_price: {trade.entry_price}\n"
        f"  stop_loss_price: {trade.stop_loss_price}\n"
        f"  take_profit_price: {trade.take_profit_price}\n"
        f"  unrealized_pnl_quote (best-effort): "
        f"{trade.realized_pnl_quote or '(open)'}\n"
    )


def _parse_decision_payload(
    content: str,
) -> tuple[NewsDecision, str] | None:
    """Strict JSON parse. Returns (decision, justification) or None."""
    raw = (content or "").strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    raw = raw.strip()
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    decision = data.get("decision")
    justification = data.get("justification")
    if decision not in ("reduce", "close", "hold"):
        return None
    if not isinstance(justification, str) or not justification.strip():
        return None
    return decision, justification.strip()
