"""Value types for the trade lifecycle (FASE 9.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

TradeSide = Literal["long", "short"]

TradeStatus = Literal["pending", "open", "closed", "failed"]

TRADE_STATUSES: tuple[TradeStatus, ...] = ("pending", "open", "closed", "failed")

TradeEventType = Literal["created", "opened", "closed", "failed", "reconciled"]


@dataclass(frozen=True)
class Trade:
    """In-memory view of a row in ``trades``.

    Lifecycle:
      ``pending`` (just created, entry being placed)
      → ``open``  (entry filled, native stop confirmed)
      → ``closed`` (stop or take-profit triggered) | ``failed``
        (entry rejected, fill timeout, stop placement failed).
    """

    trade_id: int
    signal_id: int
    ticker: str
    side: TradeSide
    size: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    opened_at: datetime
    status: TradeStatus
    exchange_id: str

    take_profit_price: Decimal | None = None
    exit_price: Decimal | None = None
    closed_at: datetime | None = None
    realized_pnl_quote: Decimal | None = None
    fees_paid_quote: Decimal = Decimal(0)
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class TradeInputs:
    """Required parameters to call ``TradeRepository.add``.

    Pulled out so the executor can build it once and pass it through
    without naming every kwarg.
    """

    signal_id: int
    ticker: str
    side: TradeSide
    size: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    exchange_id: str

    take_profit_price: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def is_terminal_trade_status(status: TradeStatus) -> bool:
    return status in {"closed", "failed"}
