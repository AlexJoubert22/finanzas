"""Polls the exchange for fill confirmation of a submitted order.

FASE 9.3 uses simple polling because the WebSocket fill stream lands
in FASE 23. This module is the seam: when 23 ships, swap polling for
``watch_my_trades`` without changing the executor surface.

Default cadence: 2 s between polls, 30 s total timeout. Conservative
for testnet (slow during peak hours) and well within the patience the
operator has when watching a SEMI_AUTO confirmation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from mib.logger import logger
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderResult, OrderStatus, is_terminal_status

if TYPE_CHECKING:  # pragma: no cover
    from mib.sources.ccxt_trader import CCXTTrader


@dataclass(frozen=True)
class FillResult:
    """Outcome of :meth:`FillDetector.wait_for_fill`."""

    filled: bool
    filled_amount: Decimal
    """Best-effort filled amount. ``Decimal(0)`` on non-fills."""

    final_status: str
    """The order's status when polling stopped: filled, cancelled,
    rejected, failed, or 'timeout' if the polling clock ran out."""

    reason: str | None = None
    raw_response: dict[str, Any] | None = None


class FillDetector:
    """Polls a submitted order until terminal state or timeout."""

    def __init__(
        self,
        trader: CCXTTrader,
        order_repo: OrderRepository,
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._trader = trader
        self._order_repo = order_repo
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds

    async def wait_for_fill(self, order_db_id: int) -> FillResult:
        """Block until the order is filled, terminal, or timeout.

        On every poll, refreshes the order in DB by calling
        ``transition`` for any state change so the audit trail
        captures the lifecycle ('partially_filled' included).

        ``filled_amount`` is parsed from the exchange's response
        ``filled`` field. Spot orders typically fill atomically;
        partial fills come through with ``status='partially_filled'``
        until the final ``filled`` event.
        """
        local = await self._order_repo.get(order_db_id)
        if local is None:
            return FillResult(
                filled=False,
                filled_amount=Decimal(0),
                final_status="not_found",
                reason=f"order #{order_db_id} not found in DB",
            )
        if is_terminal_status(local.status):
            return self._build_result(local)
        if local.exchange_order_id is None:
            return FillResult(
                filled=False,
                filled_amount=Decimal(0),
                final_status=local.status,
                reason="no exchange_order_id — order never reached the exchange",
            )

        deadline = datetime.now(UTC).timestamp() + self._timeout
        symbol = self._symbol_from_payload(local)
        last_status: OrderStatus = local.status

        while datetime.now(UTC).timestamp() < deadline:
            await asyncio.sleep(self._poll_interval)
            try:
                response = await self._trader.fetch_order(
                    symbol,
                    exchange_order_id=local.exchange_order_id,
                    client_order_id=local.client_order_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "fill_detector: fetch_order failed for #{}: {}",
                    order_db_id,
                    exc,
                )
                continue

            ex_status = (response.get("status") or "").lower()
            new_status = _ccxt_to_local_status(ex_status)
            if new_status is not None and new_status != last_status:
                # Capture transition in audit trail.
                await self._order_repo.transition(
                    order_db_id,
                    new_status,
                    actor="fill_detector",
                    event_type=new_status,
                    raw_response=response,
                )
                last_status = new_status

            if new_status is not None and is_terminal_status(new_status):
                refreshed = await self._order_repo.get(order_db_id)
                if refreshed is None:
                    break
                return self._build_result(refreshed, raw=response)

        # Timed out without terminal state.
        logger.info(
            "fill_detector: timeout for order #{} after {}s; final status={}",
            order_db_id,
            self._timeout,
            last_status,
        )
        return FillResult(
            filled=False,
            filled_amount=Decimal(0),
            final_status="timeout",
            reason=f"polling timed out after {self._timeout}s with status={last_status}",
        )

    @staticmethod
    def _symbol_from_payload(order: OrderResult) -> str:  # noqa: ARG004
        # The OrderResult doesn't carry symbol directly; consumers know
        # it from the signal. We fall back to "" — Binance requires the
        # symbol on fetch_order so the executor (FASE 9.6) will pass
        # it via a wrapper before delegating here.
        return ""

    @staticmethod
    def _build_result(
        order: OrderResult, *, raw: dict[str, Any] | None = None
    ) -> FillResult:
        filled_amount = Decimal(0)
        if raw is not None:
            f = raw.get("filled")
            if f is not None:
                filled_amount = Decimal(str(f))
        elif order.status == "filled":
            filled_amount = order.amount
        return FillResult(
            filled=order.status == "filled",
            filled_amount=filled_amount,
            final_status=order.status,
            reason=order.reason,
            raw_response=raw,
        )


def _ccxt_to_local_status(ccxt_status: str) -> OrderStatus | None:
    """Translate ccxt's status vocabulary into our enum.

    ccxt: 'open', 'closed', 'canceled', 'expired', 'rejected'.
    'partial' shows up via the ``filled``/``amount`` ratio rather
    than a dedicated status; we infer 'partially_filled' from the
    response payload in the executor (FASE 9.6) — for now we map
    only the simple cases.
    """
    if ccxt_status == "closed":
        return "filled"
    if ccxt_status in ("canceled", "expired"):
        return "cancelled"
    if ccxt_status == "rejected":
        return "rejected"
    return None
