"""Standard backtest metrics (FASE 12.3).

Pure functions over a list of :class:`BacktestTrade`. No DB, no I/O —
compute_metrics is called once at the end of a run by the engine /
endpoint to produce a :class:`BacktestMetrics` payload.

Decimal end-to-end: returns are Decimal, Sharpe / Sortino are Decimal
with 8-decimal scale. ``std=0`` cases collapse to ``Decimal(0)``
(no NaN, no Infinity in stored output) so the operator dashboard
doesn't render garbage.

R-multiple per trade:
  R = (exit_price - entry_price) / |entry_price - invalidation_price|
  signed by side (long: positive when win, short: positive when win).

Profit factor:
  PF = sum(positive_pnl) / |sum(negative_pnl)|
  Edge cases:
  - all winners (no losers) → PF = INFINITY_SENTINEL (10^9)
  - all losers (no winners) → PF = Decimal(0)
  - empty trades → PF = Decimal(0)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from mib.backtest.types import BacktestTrade

#: Used for "infinite" profit factor (all winners). Avoids Decimal
#: Infinity in JSON / DB columns. Operator reads "1e9" as "no losers".
INFINITY_SENTINEL: Final[Decimal] = Decimal("1000000000")

#: Trading days per year for annualisation. Crypto trades 24/7 → 365.
#: Equity backtests would override to 252.
DEFAULT_PERIODS_PER_YEAR: Final[int] = 365

#: Bins for the R-multiple histogram (left-inclusive, right-exclusive
#: except the open-ended ends).
_R_BINS: Final[tuple[tuple[str, Decimal | None, Decimal | None], ...]] = (
    ("<-2R", None, Decimal(-2)),
    ("-2R..-1R", Decimal(-2), Decimal(-1)),
    ("-1R..0R", Decimal(-1), Decimal(0)),
    ("0R..1R", Decimal(0), Decimal(1)),
    ("1R..2R", Decimal(1), Decimal(2)),
    (">=2R", Decimal(2), None),
)


@dataclass(frozen=True)
class BacktestMetrics:
    """Standard backtest summary. Decimal-typed for safe JSON storage."""

    total_trades: int
    winners: int
    losers: int
    win_rate: Decimal
    profit_factor: Decimal
    max_drawdown_abs: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    avg_r_multiple: Decimal
    expectancy: Decimal
    """``(win_rate * avg_win) - (loss_rate * avg_loss)`` — average
    quote-currency return per trade."""

    total_pnl: Decimal
    fees_paid_total: Decimal
    r_distribution: dict[str, int] = field(default_factory=dict)
    per_strategy: dict[str, BacktestMetrics] = field(default_factory=dict)
    per_ticker: dict[str, BacktestMetrics] = field(default_factory=dict)


# ─── Public API ──────────────────────────────────────────────────────


def compute_metrics(
    trades: list[BacktestTrade],
    *,
    initial_capital: Decimal | None = None,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    include_breakdowns: bool = True,
) -> BacktestMetrics:
    """Aggregate metrics over the full trade list.

    ``initial_capital`` is needed for max-drawdown-pct; if omitted the
    pct is reported relative to peak equity (= max running cumulative
    PnL).

    ``include_breakdowns=False`` is used by the recursive per-strategy
    / per-ticker branches to avoid infinite descent.
    """
    if not trades:
        return _empty_metrics()

    total_trades = len(trades)
    pnls = [t.realized_pnl_quote for t in trades]
    winners = sum(1 for p in pnls if p > 0)
    losers = sum(1 for p in pnls if p < 0)
    total_pnl = sum(pnls, Decimal(0))
    fees = sum((t.fees_paid_quote for t in trades), Decimal(0))

    win_rate = (
        (Decimal(winners) / Decimal(total_trades)).quantize(Decimal("0.00000001"))
        if total_trades > 0
        else Decimal(0)
    )

    profit_factor = _profit_factor(pnls)
    max_dd_abs, max_dd_pct = _max_drawdown(
        pnls=pnls, initial_capital=initial_capital
    )

    returns = _per_trade_returns(trades=trades, initial_capital=initial_capital)
    sharpe = _sharpe_annualized(returns=returns, periods_per_year=periods_per_year)
    sortino = _sortino_annualized(
        returns=returns, periods_per_year=periods_per_year
    )

    r_multiples = [_r_multiple(t) for t in trades]
    avg_r = (
        (sum(r_multiples, Decimal(0)) / Decimal(total_trades)).quantize(
            Decimal("0.00000001")
        )
        if total_trades > 0
        else Decimal(0)
    )
    expectancy = _expectancy(pnls=pnls)
    r_dist = _r_distribution(r_multiples)

    per_strategy: dict[str, BacktestMetrics] = {}
    per_ticker: dict[str, BacktestMetrics] = {}
    if include_breakdowns:
        per_strategy = _group_metrics(
            trades, key=lambda t: t.strategy_id,
            initial_capital=initial_capital,
            periods_per_year=periods_per_year,
        )
        per_ticker = _group_metrics(
            trades, key=lambda t: t.ticker,
            initial_capital=initial_capital,
            periods_per_year=periods_per_year,
        )

    return BacktestMetrics(
        total_trades=total_trades,
        winners=winners,
        losers=losers,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown_abs=max_dd_abs,
        max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        avg_r_multiple=avg_r,
        expectancy=expectancy,
        total_pnl=total_pnl,
        fees_paid_total=fees,
        r_distribution=r_dist,
        per_strategy=per_strategy,
        per_ticker=per_ticker,
    )


# ─── Pure helpers ────────────────────────────────────────────────────


def _empty_metrics() -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=0,
        winners=0,
        losers=0,
        win_rate=Decimal(0),
        profit_factor=Decimal(0),
        max_drawdown_abs=Decimal(0),
        max_drawdown_pct=Decimal(0),
        sharpe_ratio=Decimal(0),
        sortino_ratio=Decimal(0),
        avg_r_multiple=Decimal(0),
        expectancy=Decimal(0),
        total_pnl=Decimal(0),
        fees_paid_total=Decimal(0),
        r_distribution={name: 0 for name, *_ in _R_BINS},
    )


def _profit_factor(pnls: list[Decimal]) -> Decimal:
    gross_win = sum((p for p in pnls if p > 0), Decimal(0))
    gross_loss_abs = sum((-p for p in pnls if p < 0), Decimal(0))
    if gross_loss_abs == 0:
        return INFINITY_SENTINEL if gross_win > 0 else Decimal(0)
    return (gross_win / gross_loss_abs).quantize(Decimal("0.00000001"))


def _max_drawdown(
    *, pnls: list[Decimal], initial_capital: Decimal | None
) -> tuple[Decimal, Decimal]:
    """Walk the cumulative PnL track, find the largest peak-to-trough drop.

    ``max_drawdown_abs`` is in quote currency; ``max_drawdown_pct`` is
    relative to ``initial_capital`` if provided, otherwise relative
    to the peak equity at the time of the drop.
    """
    base = initial_capital if initial_capital is not None else Decimal(0)
    cum = base
    peak = base
    max_dd_abs = Decimal(0)
    max_dd_pct = Decimal(0)
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        drawdown = peak - cum
        if drawdown > max_dd_abs:
            max_dd_abs = drawdown
            denom = peak if peak > 0 else (
                initial_capital if (initial_capital and initial_capital > 0) else Decimal(1)
            )
            max_dd_pct = (drawdown / denom).quantize(Decimal("0.00000001"))
    return (
        max_dd_abs.quantize(Decimal("0.00000001")),
        max_dd_pct,
    )


def _per_trade_returns(
    *, trades: list[BacktestTrade], initial_capital: Decimal | None
) -> list[Decimal]:
    """Per-trade pct return relative to ``initial_capital`` (or 1 if absent).

    Used as the "period return" series for Sharpe/Sortino. Treating each
    trade as a period is the standard approximation when the backtester
    doesn't expose a daily PnL track; the equity-curve module (12.4)
    can override later if needed.
    """
    base = initial_capital if (initial_capital and initial_capital > 0) else Decimal(1)
    return [(t.realized_pnl_quote / base) for t in trades]


def _sharpe_annualized(
    *, returns: list[Decimal], periods_per_year: int
) -> Decimal:
    if not returns:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / Decimal(len(returns))
    std = _decimal_sqrt(variance)
    if std == 0:
        return Decimal(0)
    sharpe = (mean / std) * _decimal_sqrt(Decimal(periods_per_year))
    return sharpe.quantize(Decimal("0.00000001"))


def _sortino_annualized(
    *, returns: list[Decimal], periods_per_year: int
) -> Decimal:
    if not returns:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    downside = [r for r in returns if r < 0]
    if not downside:
        # All non-negative returns. Sortino is undefined; return
        # INFINITY_SENTINEL when mean is positive, 0 otherwise.
        return INFINITY_SENTINEL if mean > 0 else Decimal(0)
    dvar = sum((d**2 for d in downside), Decimal(0)) / Decimal(len(returns))
    dstd = _decimal_sqrt(dvar)
    if dstd == 0:
        return Decimal(0)
    sortino = (mean / dstd) * _decimal_sqrt(Decimal(periods_per_year))
    return sortino.quantize(Decimal("0.00000001"))


def _r_multiple(trade: BacktestTrade) -> Decimal:
    risk_per_unit = abs(trade.entry_price - trade.invalidation_price)
    if risk_per_unit == 0:
        return Decimal(0)
    if trade.side == "long":
        gross = trade.exit_price - trade.entry_price
    else:
        gross = trade.entry_price - trade.exit_price
    return (gross / risk_per_unit).quantize(Decimal("0.00000001"))


def _r_distribution(r_multiples: list[Decimal]) -> dict[str, int]:
    bins = {name: 0 for name, *_ in _R_BINS}
    for r in r_multiples:
        bins[_classify_r(r)] += 1
    return bins


def _classify_r(r: Decimal) -> str:
    for name, low, high in _R_BINS:
        if low is None and r < (high or Decimal(0)):
            return name
        if high is None and low is not None and r >= low:
            return name
        if (
            low is not None
            and high is not None
            and low <= r < high
        ):
            return name
    # Fallback (shouldn't happen given the bin coverage).
    return ">=2R"


def _expectancy(*, pnls: list[Decimal]) -> Decimal:
    if not pnls:
        return Decimal(0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = Decimal(len(pnls))
    win_rate = Decimal(len(wins)) / n
    loss_rate = Decimal(len(losses)) / n
    avg_win = sum(wins, Decimal(0)) / Decimal(len(wins)) if wins else Decimal(0)
    avg_loss_abs = (
        sum((-p for p in losses), Decimal(0)) / Decimal(len(losses))
        if losses
        else Decimal(0)
    )
    return ((win_rate * avg_win) - (loss_rate * avg_loss_abs)).quantize(
        Decimal("0.00000001")
    )


def _group_metrics(
    trades: list[BacktestTrade],
    *,
    key: Callable[[BacktestTrade], str],
    initial_capital: Decimal | None,
    periods_per_year: int,
) -> dict[str, BacktestMetrics]:
    groups: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {
        k: compute_metrics(
            v,
            initial_capital=initial_capital,
            periods_per_year=periods_per_year,
            include_breakdowns=False,
        )
        for k, v in groups.items()
    }


def _decimal_sqrt(x: Decimal) -> Decimal:
    """Decimal-only square root via Newton's method.

    We avoid float math for determinism and to keep the metrics
    reproducible across platforms. 30 iterations converges well past
    8-decimal precision for the magnitudes we encounter.
    """
    if x <= 0:
        return Decimal(0)
    g = x / Decimal(2)
    for _ in range(30):
        if g == 0:
            return Decimal(0)
        next_g = (g + x / g) / Decimal(2)
        if next_g == g:
            return next_g
        g = next_g
    return g
