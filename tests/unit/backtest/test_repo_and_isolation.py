"""Tests for :class:`BacktestRunRepository` + isolation guardian (FASE 12.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from mib.backtest.engine import Backtester
from mib.backtest.fill_simulator import NoFillSimulator
from mib.backtest.repo import (
    BacktestRunInput,
    BacktestRunRepository,
)
from mib.backtest.types import BacktestBar, BacktestSettings
from mib.db.models import (
    AICall,
    AIValidationRow,
    BacktestRunRow,
    NewsReactionRow,
    OrderRow,
    OrderStatusEvent,
    RiskDecisionRow,
    SignalRow,
    SignalStatusEvent,
    TradeRow,
    TradeStatusEvent,
)
from mib.db.session import async_session_factory
from mib.models.market import Candle, TechnicalSnapshot

#: Tables the backtester MUST NEVER touch. Snapshotting + comparing
#: row counts before/after a run is the canary for the isolation rule.
_PROD_TABLES = (
    SignalRow,
    SignalStatusEvent,
    OrderRow,
    OrderStatusEvent,
    TradeRow,
    TradeStatusEvent,
    RiskDecisionRow,
    AICall,
    AIValidationRow,
    NewsReactionRow,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _payload(*, strategy_id: str = "scanner.oversold.v1") -> BacktestRunInput:
    return BacktestRunInput(
        strategy_id=strategy_id,
        universe=["BTC/USDT"],
        date_range_start="2026-01-01",
        date_range_end="2026-01-31",
        initial_capital_quote=Decimal("1000"),
        final_equity_quote=Decimal("1050"),
        params={"k_invalidation": 1.5, "r_multiples": [1.0, 3.0]},
        slippage_config={"fixed_bps": 5},
        metrics={"profit_factor": "2.5", "win_rate": "0.6"},
        equity_curve_path="/tmp/run-1-curve.json",
        total_trades=5,
        ran_at=_now(),
        ran_by_actor="test",
        runtime_seconds=Decimal("3.2"),
        random_seed=42,
    )


# ─── Repo ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_returns_pk_and_persists_full_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = BacktestRunRepository(async_session_factory)
    pk = await repo.add(_payload())
    assert pk > 0
    fetched = await repo.get_by_id(pk)
    assert fetched is not None
    assert fetched.strategy_id == "scanner.oversold.v1"
    assert fetched.universe == ["BTC/USDT"]
    assert fetched.metrics == {"profit_factor": "2.5", "win_rate": "0.6"}
    assert fetched.random_seed == 42
    assert fetched.runtime_seconds == Decimal("3.2000")


@pytest.mark.asyncio
async def test_get_by_id_unknown_returns_none(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = BacktestRunRepository(async_session_factory)
    assert await repo.get_by_id(999) is None


@pytest.mark.asyncio
async def test_list_by_strategy_orders_descending(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = BacktestRunRepository(async_session_factory)
    base = _now() - timedelta(hours=2)
    p1 = _payload()
    p2 = _payload()
    p3 = _payload(strategy_id="scanner.breakout.v1")
    await repo.add(BacktestRunInput(**{**p1.__dict__, "ran_at": base}))
    await repo.add(BacktestRunInput(**{**p2.__dict__, "ran_at": base + timedelta(hours=1)}))
    await repo.add(BacktestRunInput(**{**p3.__dict__, "ran_at": base + timedelta(hours=2)}))

    listed = await repo.list_by_strategy("scanner.oversold.v1", limit=10)
    assert len(listed) == 2
    assert listed[0].ran_at > listed[1].ran_at
    # Strategy filter excludes the breakout one.
    assert all(r.strategy_id == "scanner.oversold.v1" for r in listed)


@pytest.mark.asyncio
async def test_repo_is_append_only_re_add_creates_new_row(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Same input twice → two distinct rows. There is no UPDATE path."""
    repo = BacktestRunRepository(async_session_factory)
    p1 = _payload()
    p2 = _payload()
    id1 = await repo.add(p1)
    id2 = await repo.add(p2)
    assert id1 != id2
    async with async_session_factory() as session:
        count = (
            await session.scalars(select(func.count(BacktestRunRow.id)))
        ).one()
        assert count == 2


# ─── Isolation guardian ─────────────────────────────────────────────


def _series(n_bars: int = 25) -> list[BacktestBar]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars: list[BacktestBar] = []
    for i in range(n_bars):
        ts = base + timedelta(hours=i)
        bars.append(
            BacktestBar(
                candle=Candle(
                    timestamp=ts,
                    open=100.0 + i * 0.01,
                    high=100.5 + i * 0.01,
                    low=99.5 + i * 0.01,
                    close=100.0 + i * 0.01,
                    volume=5000.0 if i == n_bars - 1 else 1000.0,
                ),
                indicators=TechnicalSnapshot(rsi_14=22.0, atr_14=2.0),
            )
        )
    return bars


@pytest.mark.asyncio
async def test_backtest_does_not_write_to_production_tables(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """GUARDIAN TEST: a backtester run MUST NOT touch any production
    table. Snapshot row counts before/after; if any of the protected
    tables grew by even 1 row, FASE 12 is broken.
    """
    # 1) Pre-snapshot: every protected table starts at 0 in fresh_db.
    pre_counts = await _snapshot_row_counts()

    # 2) Run a real backtest with a real feed — engine produces
    #    BacktestTrade objects in memory only. The isolation rule:
    #    the engine must not invoke any production repository.
    bt = Backtester(fill_simulator=NoFillSimulator())
    report = bt.run(
        preset="oversold",
        feed={"BTC/USDT": _series(n_bars=25)},
        settings=BacktestSettings(
            initial_capital_quote=Decimal("1000"),
            risk_per_trade_pct=Decimal("0.01"),
            fee_pct=Decimal("0"),
        ),
    )
    # Sanity: the run actually produced something.
    assert report.bars_processed > 0

    # 3) Even persisting the run should only touch backtest_runs.
    repo = BacktestRunRepository(async_session_factory)
    pk = await repo.add(_payload())
    assert pk > 0

    # 4) Post-snapshot: production tables UNCHANGED, backtest_runs +1.
    post_counts = await _snapshot_row_counts()
    for table_cls in _PROD_TABLES:
        pre = pre_counts.get(table_cls.__tablename__, 0)
        post = post_counts.get(table_cls.__tablename__, 0)
        assert post == pre, (
            f"isolation breach: {table_cls.__tablename__} pre={pre} post={post}"
        )

    # backtest_runs grew by exactly 1.
    assert post_counts["backtest_runs"] == pre_counts.get("backtest_runs", 0) + 1


async def _snapshot_row_counts() -> dict[str, int]:
    """Count rows for every table the isolation guardian cares about."""
    counts: dict[str, int] = {}
    async with async_session_factory() as session:
        for table_cls in (*_PROD_TABLES, BacktestRunRow):
            n = (
                await session.scalars(
                    select(func.count()).select_from(table_cls.__table__)
                )
            ).one()
            counts[table_cls.__tablename__] = int(n or 0)
    return counts
