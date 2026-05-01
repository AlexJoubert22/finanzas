"""Equity curve generation (FASE 12.4).

The curve is the core artefact 12.6 ships as a PNG to the operator. It
also feeds the metrics layer's drawdown calculation when the metrics
module is asked for a higher-resolution drawdown than the per-trade
approximation in 12.3.

Two curves are computed in lock-step:

- ``equity_with_fees``    = initial + Σ realized_pnl − Σ fees
- ``equity_without_fees`` = initial + Σ realized_pnl

The "without fees" line is the strategy's gross return — useful for
operators evaluating raw edge separately from execution friction.

Bar-resolution sampling: optionally pass ``bar_timestamps`` for the
operator's view at every bar (flat between trades). Without it, the
curve has one point at each trade close + an initial point at the
first trade's open (or just the initial point if no trades).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from mib.backtest.types import BacktestTrade


@dataclass(frozen=True)
class EquityPoint:
    """One sample on the equity curve."""

    timestamp: datetime
    equity_with_fees: Decimal
    equity_without_fees: Decimal
    realized_pnl_cumulative: Decimal
    fees_cumulative: Decimal


def build_equity_curve(
    *,
    initial_capital: Decimal,
    trades: list[BacktestTrade],
    bar_timestamps: list[datetime] | None = None,
) -> list[EquityPoint]:
    """Compose the curve sample-by-sample.

    The trades MUST already be sorted by ``exit_at``; we trust the
    caller to honour that (the engine emits them in order). Re-sorting
    here would silently mask a producer bug.

    Returns at minimum one point at ``initial_capital``. With trades,
    one point per trade close. With ``bar_timestamps``, additional
    "flat" points are emitted at each bar so the curve renders as a
    proper time series instead of just trade-close jumps.
    """
    if not trades and not bar_timestamps:
        # Caller can also pass nothing — return a single anchor point
        # so PNG render doesn't crash on an empty list.
        return [
            EquityPoint(
                timestamp=datetime.now().astimezone().replace(tzinfo=None),
                equity_with_fees=initial_capital,
                equity_without_fees=initial_capital,
                realized_pnl_cumulative=Decimal(0),
                fees_cumulative=Decimal(0),
            )
        ]

    # Anchor: timestamp = first trade's entry_at OR earliest bar OR
    # arbitrary 1970-epoch (caller can later replace).
    anchor_ts = _anchor_timestamp(trades=trades, bar_timestamps=bar_timestamps)
    anchor = EquityPoint(
        timestamp=anchor_ts,
        equity_with_fees=initial_capital,
        equity_without_fees=initial_capital,
        realized_pnl_cumulative=Decimal(0),
        fees_cumulative=Decimal(0),
    )
    points: list[EquityPoint] = [anchor]

    cum_pnl = Decimal(0)
    cum_fees = Decimal(0)
    trade_idx = 0

    timestamps = bar_timestamps if bar_timestamps else [t.exit_at for t in trades]

    for ts in timestamps:
        # Advance cum_pnl / cum_fees to include every trade closed
        # at-or-before ``ts`` (handles the case where multiple trades
        # close at the same bar).
        while trade_idx < len(trades) and trades[trade_idx].exit_at <= ts:
            cum_pnl += trades[trade_idx].realized_pnl_quote
            cum_fees += trades[trade_idx].fees_paid_quote
            trade_idx += 1
        equity_no_fees = initial_capital + cum_pnl
        equity_with_fees = equity_no_fees - cum_fees
        points.append(
            EquityPoint(
                timestamp=ts,
                equity_with_fees=equity_with_fees.quantize(Decimal("0.00000001")),
                equity_without_fees=equity_no_fees.quantize(
                    Decimal("0.00000001")
                ),
                realized_pnl_cumulative=cum_pnl.quantize(Decimal("0.00000001")),
                fees_cumulative=cum_fees.quantize(Decimal("0.00000001")),
            )
        )

    # Tail: any remaining trades closed AFTER the last bar timestamp
    # (rare; a feed cropped before the last close). Add a final point.
    while trade_idx < len(trades):
        t = trades[trade_idx]
        cum_pnl += t.realized_pnl_quote
        cum_fees += t.fees_paid_quote
        equity_no_fees = initial_capital + cum_pnl
        equity_with_fees = equity_no_fees - cum_fees
        points.append(
            EquityPoint(
                timestamp=t.exit_at,
                equity_with_fees=equity_with_fees.quantize(Decimal("0.00000001")),
                equity_without_fees=equity_no_fees.quantize(
                    Decimal("0.00000001")
                ),
                realized_pnl_cumulative=cum_pnl.quantize(Decimal("0.00000001")),
                fees_cumulative=cum_fees.quantize(Decimal("0.00000001")),
            )
        )
        trade_idx += 1

    return points


# ─── Pure helpers ────────────────────────────────────────────────────


def _anchor_timestamp(
    *,
    trades: list[BacktestTrade],
    bar_timestamps: list[datetime] | None,
) -> datetime:
    """Pick the earliest sensible anchor timestamp."""
    if bar_timestamps:
        return bar_timestamps[0]
    if trades:
        return trades[0].entry_at
    return datetime.now().astimezone().replace(tzinfo=None)
