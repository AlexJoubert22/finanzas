"""Walk-forward optimisation harness (FASE 12.7).

The danger of vanilla parameter optimisation is overfitting: if you
let the same data set both pick parameters and score them, the
optimal config will look brilliant in-sample and fail out-of-sample.
Walk-forward validates the parameter-selection process itself, not
just the parameters: at each window, the OPTIMIZER picks the best
params on the train slice, then ``score`` is computed on the test
slice. The aggregate test metric tells you whether the *strategy*
generalises, and the per-window optimal-param dispersion tells you
whether the *parameters* are stable.

Param-stability score:
  std_dev_optimal_params / mean_optimal_params (across windows)

  - ``< 0.2`` → ``"robust"``: parameters change <20% across windows.
  - ``> 0.5`` → ``"fragile"``: parameters change wildly. Strategy
    is overfit to in-sample noise.
  - else     → ``"moderate"``.

The harness is **abstract over the scoring function** so 12.7 plugs
into the existing FASE 12.3 metrics. The objective is one of
``"sharpe_ratio" | "profit_factor" | "expectancy"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from typing import Any, Literal

ObjectiveName = Literal["sharpe_ratio", "profit_factor", "expectancy"]
"""Names of the metrics fields the optimiser knows how to maximise."""

#: When optimal params don't drift more than this fraction across
#: windows, we call the strategy ``"robust"``.
ROBUST_STABILITY_THRESHOLD: Decimal = Decimal("0.2")

#: When the dispersion exceeds this, the strategy is ``"fragile"``.
FRAGILE_STABILITY_THRESHOLD: Decimal = Decimal("0.5")


@dataclass(frozen=True)
class WalkForwardConfig:
    """Window sizing knobs.

    Defaults match the FASE 12 spec: 730d train, 90d test, 30d step.
    For unit tests we'll use much smaller values so the harness can
    run on synthetic data in seconds.
    """

    train_window: timedelta = timedelta(days=730)
    test_window: timedelta = timedelta(days=90)
    step: timedelta = timedelta(days=30)
    objective: ObjectiveName = "sharpe_ratio"


@dataclass(frozen=True)
class WalkForwardWindowResult:
    """Outcome of one (train, test) cycle."""

    train_start: date
    train_end: date
    test_start: date
    test_end: date
    best_params: dict[str, Any]
    train_metric: Decimal
    test_metric: Decimal


@dataclass(frozen=True)
class WalkForwardReport:
    """Aggregate report across all walk-forward windows."""

    windows: list[WalkForwardWindowResult] = field(default_factory=list)
    avg_test_metric: Decimal = Decimal(0)
    std_test_metric: Decimal = Decimal(0)
    param_stability_score: Decimal = Decimal(0)
    """std_dev / mean of each numeric param across windows."""

    robustness_flag: Literal["robust", "fragile", "moderate", "no_data"] = (
        "no_data"
    )


# ─── Function types: scoring callable ───────────────────────────────


#: Callable signature the optimiser uses to score a (param, range)
#: combination. The harness is abstract — it doesn't care if the
#: backtester is in-process or remote, just that the function returns
#: a Decimal.
ScoreFn = Callable[
    [dict[str, Any], date, date], Decimal
]
"""``(params, range_start, range_end) -> objective_metric``."""


# ─── Optimizer ───────────────────────────────────────────────────────


class WalkForwardOptimizer:
    """Drive the train/test split + grid search over a date range."""

    def __init__(self, config: WalkForwardConfig | None = None) -> None:
        self._config = config or WalkForwardConfig()

    def run(
        self,
        *,
        param_grid: dict[str, list[Any]],
        full_date_range: tuple[date, date],
        score_fn: ScoreFn,
    ) -> WalkForwardReport:
        """Walk the windows + score each grid point.

        Pure function over (param_grid, range, score_fn). No I/O. The
        score_fn is responsible for invoking the backtester however
        the caller likes (in-process, parallel pool, persisted runs).
        """
        if not param_grid:
            raise ValueError("param_grid must contain at least one parameter")
        for k, values in param_grid.items():
            if not values:
                raise ValueError(
                    f"param_grid['{k}'] must have at least one value"
                )

        windows = list(_iterate_windows(full_date_range, self._config))
        if not windows:
            return WalkForwardReport(robustness_flag="no_data")

        results: list[WalkForwardWindowResult] = []
        param_combos = _expand_grid(param_grid)
        for train_start, train_end, test_start, test_end in windows:
            best_params: dict[str, Any] | None = None
            best_train_metric = Decimal("-1000000000")
            for params in param_combos:
                metric = score_fn(params, train_start, train_end)
                if metric > best_train_metric:
                    best_train_metric = metric
                    best_params = params
            assert best_params is not None
            test_metric = score_fn(best_params, test_start, test_end)
            results.append(
                WalkForwardWindowResult(
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    best_params=best_params,
                    train_metric=best_train_metric.quantize(
                        Decimal("0.00000001")
                    ),
                    test_metric=test_metric.quantize(Decimal("0.00000001")),
                )
            )

        avg_test = _avg([r.test_metric for r in results])
        std_test = _std([r.test_metric for r in results])
        stability = _param_stability(results)
        flag = _classify_flag(stability=stability, avg_test=avg_test)
        return WalkForwardReport(
            windows=results,
            avg_test_metric=avg_test.quantize(Decimal("0.00000001")),
            std_test_metric=std_test.quantize(Decimal("0.00000001")),
            param_stability_score=stability.quantize(Decimal("0.00000001")),
            robustness_flag=flag,
        )


# ─── Pure helpers ────────────────────────────────────────────────────


def _iterate_windows(
    full_range: tuple[date, date], config: WalkForwardConfig
) -> list[tuple[date, date, date, date]]:
    """Generate sliding (train_start, train_end, test_start, test_end).

    Walk-forward steps the cursor by ``step``; each cursor position
    creates a window if there's enough data left for the test slice.
    """
    start, end = full_range
    out: list[tuple[date, date, date, date]] = []
    cursor = start
    while True:
        train_end = cursor + config.train_window
        test_start = train_end
        test_end = test_start + config.test_window
        if test_end > end:
            break
        out.append((cursor, train_end, test_start, test_end))
        cursor = cursor + config.step
    return out


def _expand_grid(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of all param ranges."""
    keys = list(param_grid.keys())
    values_lists = [param_grid[k] for k in keys]
    return [dict(zip(keys, combo, strict=True)) for combo in product(*values_lists)]


def _avg(xs: list[Decimal]) -> Decimal:
    if not xs:
        return Decimal(0)
    return sum(xs, Decimal(0)) / Decimal(len(xs))


def _std(xs: list[Decimal]) -> Decimal:
    if len(xs) < 2:
        return Decimal(0)
    mean = _avg(xs)
    variance = sum(((x - mean) ** 2 for x in xs), Decimal(0)) / Decimal(len(xs))
    return _decimal_sqrt(variance)


def _param_stability(results: list[WalkForwardWindowResult]) -> Decimal:
    """Average ``std/|mean|`` across all numeric params.

    Non-numeric param values (booleans, strings) are ignored — the
    score is computed over the numeric slice only. If no numeric
    params exist, returns 0 (caller treats as "stable").
    """
    if not results:
        return Decimal(0)
    keys = results[0].best_params.keys()
    coefficients: list[Decimal] = []
    for k in keys:
        values: list[Decimal] = []
        for r in results:
            v = r.best_params.get(k)
            if isinstance(v, (int, float, Decimal)):
                values.append(Decimal(str(v)))
        if not values:
            continue
        mean = _avg(values)
        if mean == 0:
            continue
        std = _std(values)
        coefficients.append(abs(std / mean))
    if not coefficients:
        return Decimal(0)
    return _avg(coefficients)


def _classify_flag(
    *, stability: Decimal, avg_test: Decimal
) -> Literal["robust", "fragile", "moderate", "no_data"]:
    """Spec rules:
    - stability < 0.2 AND avg_test > 1.0 → robust
    - stability > 0.5 → fragile
    - else → moderate
    """
    if stability > FRAGILE_STABILITY_THRESHOLD:
        return "fragile"
    if stability < ROBUST_STABILITY_THRESHOLD and avg_test > Decimal("1.0"):
        return "robust"
    return "moderate"


def _decimal_sqrt(x: Decimal) -> Decimal:
    if x <= 0:
        return Decimal(0)
    g = x / Decimal(2)
    for _ in range(30):
        if g == 0:
            return Decimal(0)
        nxt = (g + x / g) / Decimal(2)
        if nxt == g:
            return nxt
        g = nxt
    return g
