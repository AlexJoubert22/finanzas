"""Fill simulator Protocol + null implementation (FASE 12.1).

12.2 lands the realistic implementation with slippage / partial fills /
latency. 12.1 defines the seam so the engine can be tested with a
deterministic stand-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from mib.backtest.types import BacktestBar

OrderType = Literal["market", "limit", "stop_market", "stop_limit"]
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class FillSimulationResult:
    """Outcome of one simulated order placement.

    ``filled`` is False when a limit order didn't cross or a partial-
    fill probability draw rejected the order. The engine then carries
    the order forward (limit) or treats it as failed (market would
    always fill in the simple model).
    """

    filled: bool
    fill_price: Decimal
    """Effective fill price after slippage. ``Decimal(0)`` when
    ``filled=False``."""

    filled_amount: Decimal
    """Amount actually filled, in base units. Equals the requested
    amount on a full fill, < requested on a partial."""

    fees_paid_quote: Decimal
    """Fee in the quote currency: ``filled_amount * fill_price *
    fee_pct``. ``Decimal(0)`` on no-fill."""

    fill_at: datetime
    """Timestamp the fill is recorded at (next bar's open by default
    so the engine ledger uses the right opened_at on entries)."""

    reason: str | None = None


@runtime_checkable
class FillSimulator(Protocol):
    """Contract every concrete fill simulator obeys."""

    def simulate_fill(
        self,
        *,
        side: OrderSide,
        order_type: OrderType,
        amount_base: Decimal,
        limit_price: Decimal | None,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,
    ) -> FillSimulationResult:
        """Synchronous, deterministic given the simulator's seed."""

    def reseed(self, seed: int) -> None:
        """Re-initialise RNG so a fresh run with the same seed
        produces byte-identical fills."""


# ─── Null implementation (12.1 testing aid) ─────────────────────────


class NoFillSimulator:
    """Always returns ``filled=True`` at next bar's open + zero fees.

    Used by 12.1 unit tests to exercise the engine without any
    slippage / partial-fill randomness. 12.2's
    :class:`SlippageFillSimulator` replaces this in production.
    """

    def __init__(self) -> None:
        self._seed: int = 0

    def simulate_fill(
        self,
        *,
        side: OrderSide,  # noqa: ARG002
        order_type: OrderType,  # noqa: ARG002
        amount_base: Decimal,
        limit_price: Decimal | None,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,  # noqa: ARG002
    ) -> FillSimulationResult:
        ref_bar = next_bar or current_bar
        fill_price = (
            limit_price
            if limit_price is not None
            else Decimal(str(ref_bar.candle.open))
        )
        return FillSimulationResult(
            filled=True,
            fill_price=fill_price,
            filled_amount=amount_base,
            fees_paid_quote=Decimal(0),
            fill_at=ref_bar.candle.timestamp,
        )

    def reseed(self, seed: int) -> None:
        self._seed = seed
