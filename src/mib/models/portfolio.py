"""Pydantic schemas for portfolio state, exposed via ``/portfolio``
and consumed by the RiskManager (FASE 8.3+) for sizing and gates.

The exchange is the source of truth: every field here mirrors what
:meth:`mib.sources.ccxt_trader.CCXTTrader.fetch_balance` and
``fetch_positions`` would return in a successful sync. Money values
use :class:`decimal.Decimal` end-to-end — float arithmetic is
forbidden in any path that touches sizing or PnL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: How the snapshot was produced. ``exchange`` is the live truth from
#: the trader; ``dry-run`` is the empty shape the skeleton returns
#: while ``trading_enabled`` is False; ``stale-cache`` is reserved for
#: future degraded modes (FASE 9 will use it when the exchange is
#: unreachable but we still return last-known state with a warning).
SnapshotSource = Literal["exchange", "dry-run", "stale-cache"]


class Balance(BaseModel):
    """Per-asset balance breakdown."""

    model_config = ConfigDict(frozen=True)

    asset: str = Field(description="Asset ticker (BTC, USDT, EUR, ...).")
    free: Decimal = Field(description="Available for new orders.")
    used: Decimal = Field(description="Locked in open orders or margin.")
    total: Decimal = Field(description="free + used.")


class Position(BaseModel):
    """An open position on a derivatives venue (futures/perp/margin).

    Spot exchanges report no positions — they only have balances. Spot
    "exposure" is computed in the RiskManager from balance composition.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Literal["long", "short"]
    amount: Decimal = Field(description="Position size in base units.")
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: float = Field(default=1.0, ge=0.0)


class PortfolioSnapshot(BaseModel):
    """Atomic snapshot of account state at ``last_synced_at``.

    Frozen so consumers in :mod:`mib.trading.risk` (FASE 8.3) can rely
    on a stable view across the duration of a single risk evaluation.
    """

    model_config = ConfigDict(frozen=True)

    balances: list[Balance] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    equity_quote: Decimal = Field(
        default=Decimal(0),
        description="Total equity in the configured quote currency (default EUR).",
    )
    last_synced_at: datetime
    source: SnapshotSource = "exchange"
