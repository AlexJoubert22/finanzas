"""Tests for :class:`DailyPostmortemRunner` (FASE 11.4)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from mib.ai.models import ProviderId, TaskType
from mib.ai.providers.base import AIResponse, AITask
from mib.db.models import DailyPostmortemRow
from mib.db.session import async_session_factory
from mib.trading.postmortem import (
    DailyPostmortemRunner,
    _parse_postmortem_payload,
    yesterday_utc_date,
)
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import TradeInputs


def _signal(strategy: str = "scanner.oversold.v1") -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=63_000.0,
        target_2=66_000.0,
        rationale="t",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id=strategy,
        confidence_ai=None,
    )


async def _seed_closed_trade(
    *,
    strategy: str = "scanner.oversold.v1",
    closed_at: datetime,
    pnl: Decimal = Decimal("1.0"),
) -> int:
    sig_repo = SignalRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    persisted = await sig_repo.add(_signal(strategy=strategy))
    trade = await trade_repo.add(
        TradeInputs(
            signal_id=persisted.id,
            ticker="BTC/USDT",
            side="long",
            size=Decimal("0.001"),
            entry_price=Decimal("60000"),
            stop_loss_price=Decimal("58800"),
            exchange_id="binance_sandbox",
            metadata={"strategy_id": strategy},
        )
    )
    await trade_repo.transition(
        trade.trade_id, "open",
        actor="seed", event_type="opened",
        expected_from_status="pending",
    )
    await trade_repo.transition(
        trade.trade_id, "closed",
        actor="seed", event_type="closed",
        expected_from_status="open",
        exit_price=Decimal("61000"),
        realized_pnl_quote=pnl,
    )
    # Manually back-date closed_at so the postmortem batch sees it.
    from mib.db.models import TradeRow  # noqa: PLC0415

    async with async_session_factory() as session, session.begin():
        row = await session.get(TradeRow, trade.trade_id)
        assert row is not None
        row.closed_at = closed_at
    return trade.trade_id


# ─── Pure helpers ────────────────────────────────────────────────────


def test_yesterday_utc_date_one_day_back() -> None:
    today = datetime.now(UTC).date()
    assert yesterday_utc_date() == today - timedelta(days=1)


def _ok_payload(
    *,
    patterns: list[dict[str, Any]] | None = None,
    outliers: list[dict[str, Any]] | None = None,
    suggestions: list[str] | None = None,
    regime_summary: str = "ranging session",
) -> str:
    return json.dumps(
        {
            "patterns": patterns
            if patterns is not None
            else [
                {
                    "description": "winners after RSI<25",
                    "trade_ids": [1, 2],
                    "category": "winner_pattern",
                }
            ],
            "aggregate_pnl_quote": 12.5,
            "outliers": outliers
            if outliers is not None
            else [{"trade_id": 3, "reason": "extreme slippage"}],
            "suggestions": suggestions
            if suggestions is not None
            else ["tighten stops on breakout strategy"],
            "regime_summary": regime_summary,
        }
    )


def test_parse_happy_path() -> None:
    parsed = _parse_postmortem_payload(_ok_payload())
    assert parsed is not None
    assert len(parsed["patterns"]) == 1
    assert parsed["regime_summary"] == "ranging session"


def test_parse_strips_markdown_fences() -> None:
    fenced = "```json\n" + _ok_payload() + "\n```"
    assert _parse_postmortem_payload(fenced) is not None


def test_parse_invalid_json_returns_none() -> None:
    assert _parse_postmortem_payload("not json") is None


def test_parse_schema_violation_returns_none() -> None:
    """patterns must be a list of objects."""
    bad = json.dumps(
        {
            "patterns": "should be a list",
            "outliers": [],
            "suggestions": [],
            "regime_summary": "x",
        }
    )
    assert _parse_postmortem_payload(bad) is None


def test_parse_suggestions_must_be_strings() -> None:
    bad = json.dumps(
        {
            "patterns": [],
            "outliers": [],
            "suggestions": [{"not": "a string"}],
            "regime_summary": "x",
        }
    )
    assert _parse_postmortem_payload(bad) is None


# ─── End-to-end with stub router ─────────────────────────────────────


class _StubRouter:
    def __init__(self, response: AIResponse) -> None:
        self._response = response
        self.calls: list[AITask] = []

    async def complete(self, task: AITask) -> AIResponse:
        self.calls.append(task)
        return self._response


@pytest.mark.asyncio
async def test_n_zero_persists_heartbeat_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """No closed trades → row persisted with trades_analyzed=0."""
    router = _StubRouter(
        AIResponse(
            success=True, content=_ok_payload(), provider=None, model="",
        )
    )
    runner = DailyPostmortemRunner(
        ai_router=router,  # type: ignore[arg-type]
        session_factory=async_session_factory,
    )
    target = date(2026, 4, 30)
    report = await runner.run_for_date(target)
    assert report.trades_analyzed == 0
    assert report.aggregate_pnl_quote == Decimal(0)
    assert report.regime_summary == "no trades closed in window"
    assert report.success is True
    # Router NOT called on N=0.
    assert router.calls == []
    # Persisted.
    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(DailyPostmortemRow).where(
                    DailyPostmortemRow.date_utc == "2026-04-30"
                )
            )
        ).first()
        assert row is not None
        assert row.trades_analyzed == 0


@pytest.mark.asyncio
async def test_with_closed_trades_calls_llm_and_persists(
    fresh_db: None,  # noqa: ARG001
) -> None:
    target = date(2026, 4, 30)
    closed_at = datetime(2026, 4, 30, 14, 0)
    await _seed_closed_trade(closed_at=closed_at, pnl=Decimal("3.0"))
    await _seed_closed_trade(
        strategy="scanner.breakout.v1", closed_at=closed_at,
        pnl=Decimal("-1.5"),
    )

    router = _StubRouter(
        AIResponse(
            success=True,
            content=_ok_payload(),
            provider=ProviderId.NVIDIA,
            model="nemotron-49b",
            latency_ms=85,
        )
    )
    runner = DailyPostmortemRunner(
        ai_router=router,  # type: ignore[arg-type]
        session_factory=async_session_factory,
    )
    report = await runner.run_for_date(target)
    assert report.trades_analyzed == 2
    assert report.aggregate_pnl_quote == Decimal("1.5")  # 3.0 + (-1.5)
    assert report.success is True
    assert report.ai_provider_used == "nvidia"
    assert report.ai_model_used == "nemotron-49b"
    assert len(report.patterns) == 1
    assert len(report.suggestions) == 1
    # Router task type is TRADE_POSTMORTEM.
    assert len(router.calls) == 1
    assert router.calls[0].task_type == TaskType.TRADE_POSTMORTEM


@pytest.mark.asyncio
async def test_router_failure_persists_row_with_error(
    fresh_db: None,  # noqa: ARG001
) -> None:
    target = date(2026, 4, 30)
    closed_at = datetime(2026, 4, 30, 10, 0)
    await _seed_closed_trade(closed_at=closed_at)

    router = _StubRouter(
        AIResponse(
            success=False,
            content="",
            provider=None,
            model="",
            error="all providers exhausted",
        )
    )
    runner = DailyPostmortemRunner(
        ai_router=router,  # type: ignore[arg-type]
        session_factory=async_session_factory,
    )
    report = await runner.run_for_date(target)
    assert report.success is False
    assert "exhausted" in (report.error_message or "")
    # Row STILL persisted (degraded mode visible to operator).
    async with async_session_factory() as session:
        row = await session.get(DailyPostmortemRow, report.row_id)
        assert row is not None
        assert row.success is False


@pytest.mark.asyncio
async def test_idempotent_re_run_returns_existing_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """UNIQUE(date_utc) → second run returns the existing row."""
    target = date(2026, 4, 30)
    router = _StubRouter(
        AIResponse(success=True, content=_ok_payload(), provider=None, model="")
    )
    runner = DailyPostmortemRunner(
        ai_router=router,  # type: ignore[arg-type]
        session_factory=async_session_factory,
    )
    first = await runner.run_for_date(target)
    second = await runner.run_for_date(target)
    assert first.row_id == second.row_id
    # Only ONE row in the DB.
    async with async_session_factory() as session:
        rows = list(
            (await session.scalars(select(DailyPostmortemRow))).all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_trades_outside_target_date_excluded(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Only ``closed_at`` inside [target_date, target_date+1) counted."""
    target = date(2026, 4, 30)
    # Inside target date.
    await _seed_closed_trade(closed_at=datetime(2026, 4, 30, 12, 0))
    # Day BEFORE target.
    await _seed_closed_trade(
        strategy="scanner.before.v1",
        closed_at=datetime(2026, 4, 29, 23, 30),
    )
    # Day AFTER target.
    await _seed_closed_trade(
        strategy="scanner.after.v1",
        closed_at=datetime(2026, 5, 1, 0, 30),
    )

    router = _StubRouter(
        AIResponse(success=True, content=_ok_payload(), provider=None, model="")
    )
    runner = DailyPostmortemRunner(
        ai_router=router,  # type: ignore[arg-type]
        session_factory=async_session_factory,
    )
    report = await runner.run_for_date(target)
    assert report.trades_analyzed == 1
