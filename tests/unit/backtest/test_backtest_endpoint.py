"""Tests for the /backtest HTTP endpoints (FASE 12.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from mib.api.app import create_app
from mib.api.dependencies import get_backtest_run_repo
from mib.backtest.repo import BacktestRunInput
from mib.db.session import async_session_factory


def _payload() -> BacktestRunInput:
    return BacktestRunInput(
        strategy_id="scanner.oversold.v1",
        universe=["BTC/USDT"],
        date_range_start="2026-01-01",
        date_range_end="2026-01-31",
        initial_capital_quote=Decimal("1000"),
        final_equity_quote=Decimal("1050"),
        params={"k_invalidation": 1.5},
        slippage_config={"fixed_bps": 5},
        metrics={
            "profit_factor": "2.5",
            "win_rate": "0.6",
            "sharpe_ratio": "1.2",
            "sortino_ratio": "1.8",
            "expectancy": "4.0",
        },
        equity_curve_path=None,
        total_trades=5,
        ran_at=datetime.now(UTC).replace(tzinfo=None),
        ran_by_actor="test",
        runtime_seconds=Decimal("1.0"),
        random_seed=7,
    )


@pytest.mark.asyncio
async def test_get_run_returns_metrics_and_curve_url(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_backtest_run_repo()
    pk = await repo.add(_payload())

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/backtest/{pk}")
    assert r.status_code == 200
    payload = r.json()
    assert payload["id"] == pk
    assert payload["strategy_id"] == "scanner.oversold.v1"
    assert payload["metrics"]["profit_factor"] == "2.5"
    assert payload["equity_curve_url"] == f"/backtest/{pk}/curve.png"


@pytest.mark.asyncio
async def test_get_run_unknown_id_returns_404(
    fresh_db: None,  # noqa: ARG001
) -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/backtest/999999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_curve_png_returns_png_bytes(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_backtest_run_repo()
    pk = await repo.add(_payload())
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/backtest/{pk}/curve.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_list_runs_filters_by_strategy(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_backtest_run_repo()
    await repo.add(_payload())
    other = _payload()
    await repo.add(BacktestRunInput(**{**other.__dict__, "strategy_id": "scanner.breakout.v1"}))

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(
            "/backtest/runs", params={"strategy_id": "scanner.oversold.v1"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert all(
        run["id"] for run in body["runs"]
    )


# ─── Telegram parsing (minimal) ──────────────────────────────────────


def test_telegram_handler_imports_clean() -> None:
    """Smoke: the Telegram handler module imports without error.

    Heavy integration tests (PTB Application + chat fixtures) are
    intentionally out of scope for this commit — the handler logic
    delegates to BacktestRunRepository which IS tested above, and to
    render_equity_curve_png which is tested in test_plotting.
    """
    from mib.telegram.handlers.backtest import backtest_cmd  # noqa: PLC0415

    assert callable(backtest_cmd)


_ = async_session_factory
