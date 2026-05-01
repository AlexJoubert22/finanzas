"""Backtester value types (FASE 12.1).

These types are the seam between the replay loop and the fill
simulator. Kept dataclass-only (frozen, no SQLAlchemy) so the
backtester can be unit-tested in isolation and plugged into a future
notebook context without dragging the rest of MIB.

Money math is :class:`decimal.Decimal` end-to-end. The candle prices
remain ``float`` because they come from the production
:class:`mib.models.market.Candle` schema, but every fill / pnl /
fees value the simulator emits is Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from mib.models.market import Candle, TechnicalSnapshot

#: Configurable knob that maps to the strategy preset name registered
#: in :class:`mib.trading.strategy.StrategyEngine`. Re-using the same
#: literal type keeps the engine ↔ backtester contract enforced by mypy.
PresetName = Literal["oversold", "breakout", "trending"]


@dataclass(frozen=True)
class BacktestBar:
    """One OHLCV bar plus the indicator snapshot valid at its close.

    The simulator must use ``next_bar`` (if any) to score fills — a
    market order at bar_t executes at bar_t+1's open, never at bar_t's
    close. This lets the backtester catch any look-ahead bias because
    ``next_bar`` is the only data the simulator can peek at, and it is
    explicit in the API.

    ``indicators`` is computed once over the full series and sampled
    per-bar by the loader so the engine does not re-run pandas-ta on
    every iteration (FASE 12 spec performance: backtests on 6 months
    of 1h bars must run in seconds, not minutes).
    """

    candle: Candle
    indicators: TechnicalSnapshot


@dataclass(frozen=True)
class BacktestSettings:
    """Per-run knobs the operator passes through ``/backtest``.

    Defaults match the strategy engine's defaults (1.5 ATR
    invalidation, R-multiples 1.0/3.0). The ``random_seed`` is wired
    through to the FillSimulator so a backtest run is fully
    reproducible — same data + same seed = byte-identical metrics.
    """

    initial_capital_quote: Decimal = Decimal("1000")
    risk_per_trade_pct: Decimal = Decimal("0.01")
    """Fraction of equity risked per trade. Default 1% to keep test
    backtests demonstrative; production wires the production-side
    sizing config separately."""

    fee_pct: Decimal = Decimal("0.001")
    """Per-fill fee, expressed as a fraction of notional. 0.1% is the
    Binance taker default. Spot-only for now; perpetuals add funding
    fees in a future phase."""

    quote_currency: str = "USDT"
    random_seed: int = 0
    """Seed for the FillSimulator's RNG. Same seed → identical fill
    decisions across runs (no_fill probability, latency jitter)."""


@dataclass(frozen=True)
class BacktestTrade:
    """A simulated round-trip (entry+exit) the backtester records."""

    ticker: str
    side: Literal["long", "short"]
    strategy_id: str
    size_base: Decimal
    entry_price: Decimal
    entry_at: datetime
    exit_price: Decimal
    exit_at: datetime
    exit_reason: Literal["stop", "target", "timeout", "end_of_data"]
    realized_pnl_quote: Decimal
    fees_paid_quote: Decimal
    invalidation_price: Decimal
    target_1_price: Decimal
    target_2_price: Decimal | None
    bars_held: int = 0
    """Number of bars the position stayed open (entry-to-exit)."""

    metadata: dict[str, str] = field(default_factory=dict)
    """Free-form provenance: which preset, k_invalidation, etc."""
