"""HTTP endpoints for the backtester (FASE 12.6).

Three endpoints:

- ``GET /backtest/{run_id}``           — read the persisted run + metrics
- ``GET /backtest/{run_id}/curve.png`` — equity curve as PNG
- ``GET /backtest/runs``               — list recent runs (paginated)

NOTE: launching new backtests via HTTP is intentionally NOT exposed
yet — running a backtest is potentially long, the operator triggers
them via the Telegram ``/backtest`` command which can run async and
ship the PNG inline. The HTTP layer here serves diagnostics + replay.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from mib.api.dependencies import get_backtest_run_repo
from mib.backtest.equity import EquityPoint
from mib.backtest.plotting import render_equity_curve_png

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/runs")
async def list_runs(
    strategy_id: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    """List recent backtest runs for ``strategy_id``."""
    repo = get_backtest_run_repo()
    runs = await repo.list_by_strategy(strategy_id, limit=limit)
    return {
        "strategy_id": strategy_id,
        "count": len(runs),
        "runs": [
            {
                "id": r.id,
                "ran_at": r.ran_at.isoformat(),
                "ran_by_actor": r.ran_by_actor,
                "universe": r.universe,
                "date_range": [r.date_range_start, r.date_range_end],
                "total_trades": r.total_trades,
                "final_equity_quote": str(r.final_equity_quote),
                "runtime_seconds": str(r.runtime_seconds),
                "random_seed": r.random_seed,
                "engine_version": r.engine_version,
            }
            for r in runs
        ],
    }


@router.get("/{run_id}")
async def get_run(run_id: int) -> dict[str, object]:
    """Read a persisted run — full metrics + parameters."""
    repo = get_backtest_run_repo()
    run = await repo.get_by_id(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run #{run_id} not found")
    return {
        "id": run.id,
        "strategy_id": run.strategy_id,
        "universe": run.universe,
        "date_range": [run.date_range_start, run.date_range_end],
        "initial_capital_quote": str(run.initial_capital_quote),
        "final_equity_quote": str(run.final_equity_quote),
        "params": run.params,
        "slippage_config": run.slippage_config,
        "metrics": run.metrics,
        "total_trades": run.total_trades,
        "ran_at": run.ran_at.isoformat(),
        "ran_by_actor": run.ran_by_actor,
        "runtime_seconds": str(run.runtime_seconds),
        "random_seed": run.random_seed,
        "engine_version": run.engine_version,
        "equity_curve_url": f"/backtest/{run.id}/curve.png",
    }


@router.get("/{run_id}/curve.png")
async def get_run_curve(run_id: int) -> Response:
    """Equity curve PNG for a persisted run.

    Loads the curve from ``equity_curve_path`` (JSON list of
    EquityPoint dicts) when present; otherwise renders an empty
    placeholder so the endpoint never returns an empty body.
    """
    repo = get_backtest_run_repo()
    run = await repo.get_by_id(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run #{run_id} not found")
    curve = _load_curve(run.equity_curve_path) if run.equity_curve_path else []
    png = render_equity_curve_png(
        curve,
        title=f"{run.strategy_id} — run #{run.id}",
    )
    return Response(content=png, media_type="image/png")


# ─── Internal ────────────────────────────────────────────────────────


def _load_curve(path: str) -> list[EquityPoint]:
    """Load EquityPoint list from a JSON file. Errors return [] so
    the PNG still renders (placeholder)."""
    import json  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out: list[EquityPoint] = []
    for entry in raw:
        try:
            out.append(
                EquityPoint(
                    timestamp=datetime.fromisoformat(entry["timestamp"]),
                    equity_with_fees=Decimal(str(entry["equity_with_fees"])),
                    equity_without_fees=Decimal(str(entry["equity_without_fees"])),
                    realized_pnl_cumulative=Decimal(
                        str(entry.get("realized_pnl_cumulative", "0"))
                    ),
                    fees_cumulative=Decimal(str(entry.get("fees_cumulative", "0"))),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out
