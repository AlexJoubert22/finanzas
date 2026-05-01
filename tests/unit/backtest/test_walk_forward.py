"""Tests for :mod:`mib.backtest.walk_forward` (FASE 12.7)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mib.backtest.walk_forward import (
    WalkForwardConfig,
    WalkForwardOptimizer,
    _expand_grid,
    _iterate_windows,
    _param_stability,
)

# ─── Pure helpers ────────────────────────────────────────────────────


def test_expand_grid_cartesian_product() -> None:
    grid = {"a": [1, 2], "b": [10, 20, 30]}
    combos = _expand_grid(grid)
    assert len(combos) == 6
    assert {"a": 1, "b": 10} in combos
    assert {"a": 2, "b": 30} in combos


def test_iterate_windows_sliding_steps() -> None:
    """train=10d, test=3d, step=5d, range=30d → multiple windows."""
    cfg = WalkForwardConfig(
        train_window=timedelta(days=10),
        test_window=timedelta(days=3),
        step=timedelta(days=5),
    )
    windows = _iterate_windows((date(2026, 1, 1), date(2026, 1, 31)), cfg)
    # First window: train [Jan 1, Jan 11), test [Jan 11, Jan 14).
    assert windows[0] == (
        date(2026, 1, 1),
        date(2026, 1, 11),
        date(2026, 1, 11),
        date(2026, 1, 14),
    )
    # Step 5d: each subsequent window starts 5 days later.
    deltas = [
        (windows[i + 1][0] - windows[i][0]).days for i in range(len(windows) - 1)
    ]
    assert all(d == 5 for d in deltas)


def test_iterate_windows_too_short_returns_empty() -> None:
    cfg = WalkForwardConfig(
        train_window=timedelta(days=30),
        test_window=timedelta(days=10),
        step=timedelta(days=5),
    )
    windows = _iterate_windows((date(2026, 1, 1), date(2026, 1, 20)), cfg)
    assert windows == []


def test_param_stability_returns_zero_for_empty() -> None:
    assert _param_stability([]) == Decimal(0)


# ─── Optimizer end-to-end ────────────────────────────────────────────


def _short_config() -> WalkForwardConfig:
    """6-month-equivalent harness: 60d train, 14d test, 7d step."""
    return WalkForwardConfig(
        train_window=timedelta(days=60),
        test_window=timedelta(days=14),
        step=timedelta(days=7),
    )


def test_robust_strategy_classified_robust() -> None:
    """Synthetic scoring: optimal params don't drift across windows
    and avg_test_metric > 1.0 → flag='robust'.

    Score function: returns ``params['p']`` directly. With grid
    ``[5, 10, 15]``, optimal is always 15 (no drift), test_metric is 15.
    """
    grid: dict[str, list[Any]] = {"p": [5, 10, 15]}

    def score(params: dict[str, Any], _s: date, _e: date) -> Decimal:
        return Decimal(params["p"])

    opt = WalkForwardOptimizer(_short_config())
    report = opt.run(
        param_grid=grid,
        full_date_range=(date(2026, 1, 1), date(2026, 7, 1)),
        score_fn=score,
    )
    assert len(report.windows) > 1
    # All best_params identical → stability=0 → robust (avg_test=15>1).
    assert report.param_stability_score == Decimal(0)
    assert report.avg_test_metric == Decimal("15.00000000")
    assert report.robustness_flag == "robust"


def test_fragile_strategy_classified_fragile() -> None:
    """Synthetic: optimal params change wildly window-to-window.

    Score depends on the train_start day-of-year so different windows
    pick different optimal p. Param_stability ends up >0.5.
    """
    grid: dict[str, list[Any]] = {"p": [10, 100, 1000]}

    def score(params: dict[str, Any], train_start: date, _e: date) -> Decimal:
        # Different train_start picks different optimum: p=10 favoured
        # in early windows, p=100 in mid, p=1000 in late.
        bucket = train_start.toordinal() % 3
        targets = [10, 100, 1000]
        return Decimal(1) if params["p"] == targets[bucket] else Decimal(0)

    opt = WalkForwardOptimizer(_short_config())
    report = opt.run(
        param_grid=grid,
        full_date_range=(date(2026, 1, 1), date(2026, 12, 31)),
        score_fn=score,
    )
    # Best params jump 10 → 100 → 1000 across windows; stability is huge.
    assert report.param_stability_score > Decimal("0.5")
    assert report.robustness_flag == "fragile"


def test_empty_range_returns_no_data_flag() -> None:
    grid: dict[str, list[Any]] = {"p": [1]}

    def score(_p: dict[str, Any], _s: date, _e: date) -> Decimal:
        return Decimal(0)

    opt = WalkForwardOptimizer(_short_config())
    report = opt.run(
        param_grid=grid,
        full_date_range=(date(2026, 1, 1), date(2026, 1, 5)),
        score_fn=score,
    )
    assert report.windows == []
    assert report.robustness_flag == "no_data"


def test_empty_param_grid_raises() -> None:
    opt = WalkForwardOptimizer(_short_config())
    with pytest.raises(ValueError, match="must contain at least one parameter"):
        opt.run(
            param_grid={},
            full_date_range=(date(2026, 1, 1), date(2026, 7, 1)),
            score_fn=lambda *_: Decimal(0),
        )


def test_empty_param_values_raises() -> None:
    opt = WalkForwardOptimizer(_short_config())
    with pytest.raises(ValueError, match="must have at least one value"):
        opt.run(
            param_grid={"p": []},
            full_date_range=(date(2026, 1, 1), date(2026, 7, 1)),
            score_fn=lambda *_: Decimal(0),
        )


def test_avg_test_metric_computed_correctly() -> None:
    """Returns 5.0 for every test window → avg_test_metric exactly 5.0."""
    grid: dict[str, list[Any]] = {"p": [1, 2, 3]}

    def score(_p: dict[str, Any], _s: date, _e: date) -> Decimal:
        return Decimal(5)

    opt = WalkForwardOptimizer(_short_config())
    report = opt.run(
        param_grid=grid,
        full_date_range=(date(2026, 1, 1), date(2026, 12, 31)),
        score_fn=score,
    )
    assert report.avg_test_metric == Decimal("5.00000000")
    # All identical → std=0.
    assert report.std_test_metric == Decimal(0)
