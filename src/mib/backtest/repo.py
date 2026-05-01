"""Append-only repository for ``backtest_runs`` (FASE 12.5).

INSERT-only. The repo never updates or deletes rows: each
``Backtester.run`` produces a new row that captures the full inputs
(params, slippage, seed) AND the outputs (metrics, equity curve
location). Re-runs with identical inputs create a new row — the
operator decides whether to dedupe semantically by reading
``random_seed`` + ``params_json``.

Strict isolation rule: this module NEVER writes to ``signals`` /
``trades`` / ``orders`` / any production table. The FASE 12.5
isolation test snapshots row counts before/after to enforce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import BacktestRunRow

#: Bumped manually when the engine semantics change in a way that
#: invalidates historical comparisons (e.g. slippage formula change).
ENGINE_VERSION: str = "1.0"


@dataclass(frozen=True)
class BacktestRunInput:
    """All the data the repo needs to persist a run."""

    strategy_id: str
    universe: list[str]
    date_range_start: str
    date_range_end: str
    initial_capital_quote: Decimal
    final_equity_quote: Decimal
    params: dict[str, Any]
    slippage_config: dict[str, Any] | None
    metrics: dict[str, Any]
    equity_curve_path: str | None
    total_trades: int
    ran_at: datetime
    ran_by_actor: str
    runtime_seconds: Decimal
    random_seed: int
    engine_version: str = ENGINE_VERSION


@dataclass(frozen=True)
class BacktestRun:
    """In-memory view of a row. The metrics/params come back as dicts
    (already deserialised) so the API/Telegram handlers don't double-
    parse JSON.
    """

    id: int
    strategy_id: str
    universe: list[str]
    date_range_start: str
    date_range_end: str
    initial_capital_quote: Decimal
    final_equity_quote: Decimal
    params: dict[str, Any]
    slippage_config: dict[str, Any] | None
    metrics: dict[str, Any]
    equity_curve_path: str | None
    total_trades: int
    ran_at: datetime
    ran_by_actor: str
    runtime_seconds: Decimal
    random_seed: int
    engine_version: str


class BacktestRunRepository:
    """INSERT-only persistence boundary for ``backtest_runs``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, payload: BacktestRunInput) -> int:
        async with self._sf() as session, session.begin():
            row = BacktestRunRow(
                strategy_id=payload.strategy_id,
                universe_json=list(payload.universe),
                date_range_start=payload.date_range_start,
                date_range_end=payload.date_range_end,
                initial_capital_quote=payload.initial_capital_quote,
                final_equity_quote=payload.final_equity_quote,
                params_json=dict(payload.params),
                slippage_config_json=(
                    dict(payload.slippage_config)
                    if payload.slippage_config is not None
                    else None
                ),
                metrics_json=dict(payload.metrics),
                equity_curve_path=payload.equity_curve_path,
                total_trades=payload.total_trades,
                ran_at=payload.ran_at,
                ran_by_actor=payload.ran_by_actor,
                runtime_seconds=payload.runtime_seconds,
                random_seed=payload.random_seed,
                engine_version=payload.engine_version,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    async def get_by_id(self, run_id: int) -> BacktestRun | None:
        async with self._sf() as session:
            row = await session.get(BacktestRunRow, run_id)
            return _to_dc(row) if row is not None else None

    async def list_by_strategy(
        self, strategy_id: str, *, limit: int = 50
    ) -> list[BacktestRun]:
        async with self._sf() as session:
            stmt = (
                select(BacktestRunRow)
                .where(BacktestRunRow.strategy_id == strategy_id)
                .order_by(BacktestRunRow.ran_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_to_dc(r) for r in rows]


# ─── Helpers ─────────────────────────────────────────────────────────


def _to_dc(row: BacktestRunRow) -> BacktestRun:
    return BacktestRun(
        id=int(row.id),
        strategy_id=row.strategy_id,
        universe=list(row.universe_json or []),
        date_range_start=row.date_range_start,
        date_range_end=row.date_range_end,
        initial_capital_quote=Decimal(str(row.initial_capital_quote)),
        final_equity_quote=Decimal(str(row.final_equity_quote)),
        params=dict(row.params_json or {}),
        slippage_config=(
            dict(row.slippage_config_json)
            if row.slippage_config_json is not None
            else None
        ),
        metrics=dict(row.metrics_json or {}),
        equity_curve_path=row.equity_curve_path,
        total_trades=int(row.total_trades),
        ran_at=row.ran_at,
        ran_by_actor=row.ran_by_actor,
        runtime_seconds=Decimal(str(row.runtime_seconds)),
        random_seed=int(row.random_seed),
        engine_version=row.engine_version,
    )
