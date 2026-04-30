"""Value types for the order execution layer (FASE 9.2).

The :class:`OrderResult` dataclass is the executor's return value:
the caller knows if the order made it to the exchange, what the
exchange returned, and what to record in the trade lifecycle.

These types are intentionally kept separate from the ORM and the
``mib.trading.signals`` types so that backtester (FASE 12) and
production share the same shape without dragging in SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

#: Order types we ever ask the exchange to place. ``stop_market``
#: lands in FASE 9.3 (native stop after fill); ``stop_limit`` is
#: reserved for future use.
OrderType = Literal["limit", "market", "stop_market", "stop_limit"]

OrderSide = Literal["buy", "sell"]

#: Lifecycle states for ``orders.status`` (denormalised cache of the
#: latest event in ``order_status_events``).
#:
#: - ``created``           — row exists in DB, no exchange call yet
#: - ``submitted``         — exchange ack received, exchange_order_id set
#: - ``partially_filled``  — exchange reports filled < amount
#: - ``filled``            — exchange reports fully filled
#: - ``cancelled``         — operator or reconcile cancelled
#: - ``rejected``          — exchange returned a 4xx/5xx; pre-submit error
#: - ``failed``            — local error before/after exchange ack (timeout)
#: - ``reconciled``        — reconciler reaped a stale ``submitted`` row
OrderStatus = Literal[
    "created",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "failed",
    "reconciled",
]

ORDER_STATUSES: tuple[OrderStatus, ...] = (
    "created",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "failed",
    "reconciled",
)

#: Action verbs that get written to ``order_status_events.event_type``.
#: ``created`` is automatic on add(). The rest are caller-driven.
OrderEventType = Literal[
    "created",
    "submitted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "failed",
    "reconciled",
]


@dataclass(frozen=True)
class OrderResult:
    """Outcome of one ``CCXTTrader.create_order`` invocation.

    All fields populated when the call hits the exchange successfully.
    On failure paths (triple seatbelt blocks, exchange rejection,
    timeout) ``exchange_order_id`` stays None and ``status`` reflects
    the failure mode.
    """

    order_id: int
    """Primary key of the row in the ``orders`` table."""

    client_order_id: str
    """Idempotency key sent to the exchange. Deterministic per
    (signal_id, side, type, amount, price)."""

    exchange_order_id: str | None
    """Id assigned by the exchange (None for dry-run / pre-submit
    failures)."""

    status: OrderStatus

    side: OrderSide
    type: OrderType
    amount: Decimal
    price: Decimal | None

    reason: str | None = None
    """Populated on ``rejected`` / ``failed`` / ``dry-run``-style
    paths. Empty string is normalised to None."""

    raw_response_json: dict[str, Any] | None = None

    decided_at: datetime | None = field(default=None)
    """When the executor finalised this result (after exchange round-
    trip if any). Useful for latency metrics."""


@dataclass(frozen=True)
class OrderInputs:
    """Parameters needed to create an order, before idempotency lookup.

    The repository hashes a stable subset of these fields to derive
    the deterministic ``client_order_id`` so retries land on the same
    DB row instead of duplicating.
    """

    signal_id: int
    symbol: str
    side: OrderSide
    type: OrderType
    amount: Decimal
    price: Decimal | None = None
    reduce_only: bool = False
    extra_params: dict[str, Any] = field(default_factory=dict)


def is_terminal_status(status: OrderStatus) -> bool:
    """True iff the status is a final, non-revisitable state.

    Used by the executor's polling loop to know when to stop waiting
    for fill events.
    """
    return status in {"filled", "cancelled", "rejected", "failed"}
