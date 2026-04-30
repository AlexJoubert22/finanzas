"""Daily Postmortem analyser (FASE 11.4).

Runs once a day at 02:00 UTC. Reads the closed trades from the last
24h, batches them through the LLM with the
:data:`SYSTEM_TRADE_POSTMORTEM_V1` prompt, and persists the analysis
into ``daily_postmortems`` (append-only, ``UNIQUE(date_utc)``).

Behaviours:

- N=0 trades → still persists a heartbeat row with
  ``trades_analyzed=0`` so the operator sees the job ran.
- Provider failures → row with ``success=False`` and the error
  message captured. The 08:00 morning report formatter (FASE 14.4)
  reads this and surfaces a degraded-mode notice.
- Re-runs for the same UTC date are idempotent: the
  ``UNIQUE(date_utc)`` constraint blows on the second insert;
  :class:`DailyPostmortemRunner.run_for_date` returns the existing
  row in that case.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.ai.models import TaskType
from mib.ai.prompts import SYSTEM_TRADE_POSTMORTEM_V1
from mib.ai.providers.base import AITask
from mib.ai.router import AIRouter
from mib.db.models import DailyPostmortemRow, TradeRow
from mib.logger import logger


@dataclass(frozen=True)
class PostmortemReport:
    """Outcome of one postmortem run (in-memory shape)."""

    date_utc: str
    trades_analyzed: int
    aggregate_pnl_quote: Decimal
    patterns: list[dict[str, Any]] = field(default_factory=list)
    outliers: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    regime_summary: str | None = None
    ai_provider_used: str | None = None
    ai_model_used: str | None = None
    success: bool = True
    error_message: str | None = None
    row_id: int | None = None


class DailyPostmortemRunner:
    """Composes the trade batch + LLM call + persistence."""

    def __init__(
        self,
        *,
        ai_router: AIRouter,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._router = ai_router
        self._sf = session_factory

    async def run_for_date(self, target_date: date) -> PostmortemReport:
        """Analyse the 24h of trades whose ``closed_at`` falls on ``target_date``.

        Idempotent: a second invocation for the same date returns the
        existing row's report (read after IntegrityError catch).
        """
        date_str = target_date.isoformat()

        # Idempotency short-circuit: do we already have a row?
        existing = await self._fetch_existing(date_str)
        if existing is not None:
            logger.info(
                "postmortem: row for {} already exists (#{}), returning",
                date_str,
                existing.id,
            )
            return _row_to_report(existing)

        trades = await self._fetch_trades_for_date(target_date)
        trades_analyzed = len(trades)
        aggregate_pnl = sum(
            (Decimal(str(t.realized_pnl_quote or 0)) for t in trades),
            Decimal(0),
        )

        if trades_analyzed == 0:
            report = PostmortemReport(
                date_utc=date_str,
                trades_analyzed=0,
                aggregate_pnl_quote=Decimal(0),
                regime_summary="no trades closed in window",
                success=True,
            )
            return await self._persist(report)

        # LLM analysis.
        llm_outcome = await self._call_llm(trades)
        report = PostmortemReport(
            date_utc=date_str,
            trades_analyzed=trades_analyzed,
            aggregate_pnl_quote=aggregate_pnl,
            patterns=llm_outcome.patterns,
            outliers=llm_outcome.outliers,
            suggestions=llm_outcome.suggestions,
            regime_summary=llm_outcome.regime_summary,
            ai_provider_used=llm_outcome.provider_used,
            ai_model_used=llm_outcome.model_used,
            success=llm_outcome.success,
            error_message=llm_outcome.error,
        )
        return await self._persist(report)

    # ─── Internal helpers ──────────────────────────────────────────

    async def _fetch_existing(
        self, date_str: str
    ) -> DailyPostmortemRow | None:
        async with self._sf() as session:
            stmt = select(DailyPostmortemRow).where(
                DailyPostmortemRow.date_utc == date_str
            )
            return (await session.scalars(stmt)).first()

    async def _fetch_trades_for_date(
        self, target_date: date
    ) -> list[TradeRow]:
        start = datetime.combine(target_date, datetime.min.time())
        end = start + timedelta(days=1)
        async with self._sf() as session:
            stmt = (
                select(TradeRow)
                .where(
                    TradeRow.status == "closed",
                    TradeRow.closed_at.is_not(None),
                    TradeRow.closed_at >= start,
                    TradeRow.closed_at < end,
                )
                .order_by(TradeRow.closed_at.asc())
            )
            return list((await session.scalars(stmt)).all())

    async def _call_llm(self, trades: list[TradeRow]) -> _LLMOutcome:
        batch = [_serialise_trade(t) for t in trades]
        user_message = (
            "Analyse the following batch of closed trades from the last "
            "24h. Use the schema documented in the system prompt.\n\n"
            f"BATCH (n={len(batch)}):\n{json.dumps(batch, default=str)}\n"
        )
        task = AITask(
            task_type=TaskType.TRADE_POSTMORTEM,
            system=SYSTEM_TRADE_POSTMORTEM_V1,
            prompt=user_message,
            temperature=0.2,
            max_tokens=1024,
        )
        t0 = time.monotonic()
        response = await self._router.complete(task)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if not response.success:
            return _LLMOutcome(
                success=False,
                provider_used=(
                    response.provider.value if response.provider else None
                ),
                model_used=response.model or None,
                error=response.error or "router_failed",
                latency_ms=response.latency_ms or latency_ms,
            )
        parsed = _parse_postmortem_payload(response.content)
        if parsed is None:
            return _LLMOutcome(
                success=False,
                provider_used=(
                    response.provider.value if response.provider else None
                ),
                model_used=response.model or None,
                error="json_parse_or_schema_error",
                latency_ms=response.latency_ms or latency_ms,
            )
        return _LLMOutcome(
            success=True,
            patterns=parsed["patterns"],
            outliers=parsed["outliers"],
            suggestions=parsed["suggestions"],
            regime_summary=parsed["regime_summary"],
            provider_used=(
                response.provider.value if response.provider else None
            ),
            model_used=response.model or None,
            latency_ms=response.latency_ms or latency_ms,
        )

    async def _persist(self, report: PostmortemReport) -> PostmortemReport:
        try:
            async with self._sf() as session, session.begin():
                row = DailyPostmortemRow(
                    date_utc=report.date_utc,
                    trades_analyzed=report.trades_analyzed,
                    aggregate_pnl_quote=report.aggregate_pnl_quote,
                    patterns_json=report.patterns,
                    outliers_json=report.outliers,
                    suggestions_json=report.suggestions,
                    regime_summary=report.regime_summary,
                    ai_provider_used=report.ai_provider_used,
                    ai_model_used=report.ai_model_used,
                    success=report.success,
                    error_message=report.error_message,
                )
                session.add(row)
                await session.flush()
                row_id = int(row.id)
            from dataclasses import replace  # noqa: PLC0415

            return replace(report, row_id=row_id)
        except IntegrityError as exc:
            logger.info(
                "postmortem: row for {} already exists ({}); returning existing",
                report.date_utc,
                exc,
            )
            existing = await self._fetch_existing(report.date_utc)
            if existing is None:
                # Should not happen — IntegrityError without an existing
                # row would mean a different constraint blew. Surface it.
                raise
            return _row_to_report(existing)


# ─── Pure helpers ───────────────────────────────────────────────────


@dataclass(frozen=True)
class _LLMOutcome:
    """Internal: parsed result from the postmortem LLM call."""

    success: bool
    patterns: list[dict[str, Any]] = field(default_factory=list)
    outliers: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    regime_summary: str | None = None
    provider_used: str | None = None
    model_used: str | None = None
    latency_ms: int = 0
    error: str | None = None


def _serialise_trade(t: TradeRow) -> dict[str, Any]:
    return {
        "id": int(t.id),
        "ticker": t.ticker,
        "side": t.side,
        "entry_price": str(t.entry_price),
        "exit_price": str(t.exit_price) if t.exit_price is not None else None,
        "size": str(t.size),
        "realized_pnl_quote": (
            str(t.realized_pnl_quote)
            if t.realized_pnl_quote is not None
            else None
        ),
        "fees_paid_quote": str(t.fees_paid_quote),
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "strategy_id": (t.metadata_json or {}).get("strategy_id"),
        "exchange_id": t.exchange_id,
    }


def _parse_postmortem_payload(content: str) -> dict[str, Any] | None:
    """Strict JSON + schema validation. Returns None on any deviation."""
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
    patterns = data.get("patterns")
    outliers = data.get("outliers")
    suggestions = data.get("suggestions")
    regime_summary = data.get("regime_summary")
    if not isinstance(patterns, list) or not all(
        isinstance(p, dict) for p in patterns
    ):
        return None
    if not isinstance(outliers, list) or not all(
        isinstance(o, dict) for o in outliers
    ):
        return None
    if not isinstance(suggestions, list) or not all(
        isinstance(s, str) for s in suggestions
    ):
        return None
    if regime_summary is not None and not isinstance(regime_summary, str):
        return None
    return {
        "patterns": patterns,
        "outliers": outliers,
        "suggestions": suggestions,
        "regime_summary": regime_summary,
    }


def _row_to_report(row: DailyPostmortemRow) -> PostmortemReport:
    return PostmortemReport(
        date_utc=row.date_utc,
        trades_analyzed=row.trades_analyzed,
        aggregate_pnl_quote=Decimal(str(row.aggregate_pnl_quote)),
        patterns=list(row.patterns_json or []),
        outliers=list(row.outliers_json or []),
        suggestions=list(row.suggestions_json or []),
        regime_summary=row.regime_summary,
        ai_provider_used=row.ai_provider_used,
        ai_model_used=row.ai_model_used,
        success=row.success,
        error_message=row.error_message,
        row_id=int(row.id),
    )


def yesterday_utc_date() -> date:
    """The date one day before today (UTC). Used by the 02:00 cron."""
    return (datetime.now(UTC) - timedelta(days=1)).date()
