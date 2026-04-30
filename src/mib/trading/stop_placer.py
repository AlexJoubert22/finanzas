"""Native stop placement after entry fill.

FASE 9.3 critical-safety component: once the entry order has filled,
a stop order with ``reduceOnly=True`` is placed at
``signal.invalidation`` to bound downside if the bot dies before the
trade closes naturally.

Order type & fallback (FASE 9.3 + post-FASE-9 hotfix):

- Primary: ``stop_market``. Cheapest semantics, fills at any price
  once the trigger is touched.
- Binance spot rejects ``stop_market`` for many symbols (error
  code -2010, "Order type not supported"). When that specific
  rejection is detected, this placer **automatically retries the
  same attempt with ``stop_limit``**, computing a limit price
  slightly past the trigger so the limit fills under stress:
  - long  (sell-to-exit): ``limit = stopPrice * 0.995``
  - short (buy-to-exit):  ``limit = stopPrice * 1.005``
- The fallback is a per-attempt internal swap: it does NOT consume
  a retry slot. The 3-retry exponential backoff still applies on
  top of whichever order type ends up landing.

Retry policy: 3 attempts with exponential backoff (1s, 2s, 4s).
Transient errors (timeout, network, 5xx) trigger retry; permanent
errors (insufficient balance, invalid params) do NOT — there's no
point retrying a 4xx that the exchange already rejected with reason.

On all 3 retries exhausted:

- ``structlog`` WARNING with full context.
- Telegram alert to admin via :class:`TelegramAlerter`.
- ``# TODO FASE 13`` comment for the eventual incident registry
  emit (``CriticalIncidentType.NATIVE_STOP_MISSING_AFTER_FILL``).

The caller (executor in 9.6) marks the trade as ``failed`` and
attempts to close the entry to avoid uncovered exposure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from mib.logger import logger
from mib.trading.alerter import TelegramAlerter
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderResult, OrderSide, is_terminal_status
from mib.trading.signals import Signal

if TYPE_CHECKING:  # pragma: no cover
    from mib.sources.ccxt_trader import CCXTTrader


@dataclass(frozen=True)
class StopPlacementResult:
    success: bool
    stop_order_id: int | None
    """Primary key of the persisted stop order (None on failure)."""

    exchange_order_id: str | None
    """Exchange-side id of the stop order."""

    attempts: int
    """How many attempts were spent (1 on first-try success)."""

    reason: str | None = None


_RETRY_BACKOFFS_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


class NativeStopPlacer:
    """Places a native stop_market with reduceOnly after fill detection."""

    def __init__(
        self,
        trader: CCXTTrader,
        order_repo: OrderRepository,
        alerter: TelegramAlerter,
        *,
        attempts: int = 3,
        backoffs: tuple[float, ...] = _RETRY_BACKOFFS_SECONDS,
    ) -> None:
        self._trader = trader
        self._order_repo = order_repo
        self._alerter = alerter
        self._attempts = attempts
        self._backoffs = backoffs

    async def place_stop_after_fill(
        self,
        signal: Signal,
        entry_order_id: int,
        *,
        filled_amount: Decimal | None = None,
    ) -> StopPlacementResult:
        """Place the protective stop. Idempotent per attempt via the
        ``_stop_attempt`` extra-param suffix in the deterministic
        client_order_id.
        """
        entry = await self._order_repo.get(entry_order_id)
        if entry is None:
            return StopPlacementResult(
                success=False,
                stop_order_id=None,
                exchange_order_id=None,
                attempts=0,
                reason=f"entry order #{entry_order_id} not found",
            )
        if entry.status not in ("filled", "partially_filled"):
            return StopPlacementResult(
                success=False,
                stop_order_id=None,
                exchange_order_id=None,
                attempts=0,
                reason=f"entry order not filled (status={entry.status})",
            )

        amount = filled_amount if filled_amount is not None else entry.amount
        if amount <= 0:
            return StopPlacementResult(
                success=False,
                stop_order_id=None,
                exchange_order_id=None,
                attempts=0,
                reason="filled amount is zero",
            )

        stop_side: OrderSide = "sell" if signal.side == "long" else "buy"
        stop_price = Decimal(str(signal.invalidation))

        last_reason: str | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                result = await self._place_with_fallback(
                    signal_id=entry.signal_id,
                    symbol=signal.ticker,
                    side=stop_side,
                    amount=amount,
                    stop_price=stop_price,
                    attempt=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                last_reason = f"unexpected: {exc.__class__.__name__}: {exc}"
                logger.warning(
                    "stop_placer: attempt {} raised {}",
                    attempt,
                    last_reason,
                )
                if not await self._maybe_backoff(attempt):
                    break
                continue

            if result.status == "submitted":
                logger.info(
                    "stop_placer: stop placed signal_id={} order_id={} attempt={}",
                    entry.signal_id,
                    result.order_id,
                    attempt,
                )
                return StopPlacementResult(
                    success=True,
                    stop_order_id=result.order_id,
                    exchange_order_id=result.exchange_order_id,
                    attempts=attempt,
                    reason=None,
                )
            if result.status == "rejected":
                # 4xx-shape from the exchange — permanent. No retry.
                last_reason = result.reason or "rejected"
                logger.warning(
                    "stop_placer: permanent rejection signal_id={}: {}",
                    entry.signal_id,
                    last_reason,
                )
                break
            if result.status == "failed":
                # Transient (timeout / network) — retry with backoff.
                last_reason = result.reason or "failed"
                logger.info(
                    "stop_placer: transient failure attempt {}/{}: {}",
                    attempt,
                    self._attempts,
                    last_reason,
                )
                if not await self._maybe_backoff(attempt):
                    break
                continue
            if result.status == "cancelled":
                # Triple seatbelt blocked the call. Retrying won't help.
                last_reason = result.reason or "blocked by triple seatbelt"
                break
            # Any other status (partially_filled, filled — shouldn't
            # happen for a fresh stop): treat as success-shaped.
            if not is_terminal_status(result.status):
                # 'created' shouldn't surface here either; fail safe.
                last_reason = f"unexpected status {result.status}"
                break
            return StopPlacementResult(
                success=True,
                stop_order_id=result.order_id,
                exchange_order_id=result.exchange_order_id,
                attempts=attempt,
                reason=None,
            )

        # All retries exhausted (or permanent fail). Alert.
        # TODO FASE 13: emit CriticalIncident type
        # NATIVE_STOP_MISSING_AFTER_FILL once the incident registry
        # lands. For now we ship a high-priority Telegram alert.
        warning_message = (
            "🚨 <b>STOP NO COLOCADO tras fill</b>\n"
            f"signal #{entry.signal_id} ticker <code>{signal.ticker}</code>\n"
            f"entry order #{entry_order_id}\n"
            f"intentos: {self._attempts}\n"
            f"último error: <code>{(last_reason or 'unknown')[:200]}</code>\n"
            "<i>Revisa manualmente y coloca un stop o cierra la posición.</i>"
        )
        try:
            await self._alerter.alert(warning_message)
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.warning("stop_placer: alerter failed: {}", exc)
        logger.warning(
            "stop_placer: ALL ATTEMPTS FAILED signal_id={} reason={}",
            entry.signal_id,
            last_reason,
        )
        return StopPlacementResult(
            success=False,
            stop_order_id=None,
            exchange_order_id=None,
            attempts=self._attempts,
            reason=last_reason or "retries_exhausted",
        )

    async def _maybe_backoff(self, attempt: int) -> bool:
        """Sleep before retry. Returns False if no further attempts left."""
        if attempt >= self._attempts:
            return False
        idx = min(attempt - 1, len(self._backoffs) - 1)
        await asyncio.sleep(self._backoffs[idx])
        return True

    async def _place_with_fallback(
        self,
        *,
        signal_id: int,
        symbol: str,
        side: OrderSide,
        amount: Decimal,
        stop_price: Decimal,
        attempt: int,
    ) -> OrderResult:
        """Place a stop. Try ``stop_market`` first; if Binance refuses
        the type, swap to ``stop_limit`` in the same attempt slot.

        The fallback does NOT consume a retry: it's an internal swap
        that runs once per outer attempt before the caller decides
        whether to back off and try again.
        """
        primary = await self._trader.create_order(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            type="stop_market",
            amount=amount,
            price=None,
            reduce_only=True,
            extra_params={
                "stopPrice": str(stop_price),
                "_stop_attempt": attempt,
            },
        )
        if not _is_type_not_supported_rejection(primary):
            return primary

        # Type-not-supported rejection — swap to stop_limit. Limit
        # price is offset past the trigger so the limit can fill
        # under the stress that triggered the stop.
        limit_price = _stop_limit_price(stop_price, side)
        logger.info(
            "stop_placer: stop_type_fallback signal_id={} from_type=stop_market "
            "to_type=stop_limit limit_price={} reason={!r}",
            signal_id,
            limit_price,
            primary.reason,
        )
        return await self._trader.create_order(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            type="stop_limit",
            amount=amount,
            price=limit_price,
            reduce_only=True,
            extra_params={
                "stopPrice": str(stop_price),
                "timeInForce": "GTC",
                "_stop_attempt": attempt,
                "_stop_type_fallback": "stop_market->stop_limit",
            },
        )


# ─── Pure helpers ───────────────────────────────────────────────────


def _is_type_not_supported_rejection(result: OrderResult) -> bool:
    """True iff the exchange refused the order specifically because
    it doesn't accept the order type on this market.

    Conservative match: only triggers on the well-known Binance
    signatures so unrelated 4xx (insufficient balance, invalid
    quantity, etc.) still break the retry loop as ``permanent``.
    """
    if result.status != "rejected":
        return False
    reason = (result.reason or "").lower()
    if not reason:
        return False
    # Binance: code -2010 + "Order type not supported" / "stop loss
    # not supported for this symbol" / "stop_market is not a valid
    # order type for the BTC/USDT market" (ccxt phrasing).
    needles = (
        "order type not supported",
        "not a valid order type",
        "not supported for this symbol",
        "-2010",
    )
    return any(n in reason for n in needles)


def _stop_limit_price(stop_price: Decimal, side: OrderSide) -> Decimal:
    """Aggressive limit price for a stop_limit fallback.

    Long-side stops are sells: limit goes 0.5% BELOW the trigger so
    a falling market keeps filling. Short-side stops are buys: limit
    goes 0.5% ABOVE the trigger so a rising market keeps filling.
    """
    # long → sell stop → limit BELOW trigger (0.995).
    # short → buy stop  → limit ABOVE trigger (1.005).
    offset = Decimal("0.995") if side == "sell" else Decimal("1.005")
    return (stop_price * offset).quantize(Decimal("0.00000001"))
