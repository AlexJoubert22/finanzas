"""End-to-end order executor (FASE 9.6).

Glues the FASE 9.2/9.3/9.4 building blocks into one coherent flow:

    1. Build a ``TradeInputs`` from the approved ``Signal`` +
       ``RiskDecision`` and persist a ``trades`` row in
       ``status='pending'``.
    2. Submit the entry order (a limit buy/sell at the entry-zone
       midpoint) via :meth:`CCXTTrader.create_order`.
    3. Wait for fill via :class:`FillDetector` (poll 2s/30s).
    4. Place the protective stop_market with reduceOnly via
       :class:`NativeStopPlacer` (3-retry exponential backoff).
    5. Backpopulate ``orders.trade_id`` for both the entry and the
       stop via :meth:`TradeRepository.link_orders_to_trade`.
    6. Transition the trade pending → open.

Failure paths (any of which transitions the trade to ``failed`` and
emits an alert):

- entry submission rejected/cancelled by triple seatbelt → no fill
  attempt, trade marked failed.
- entry timed out without fill → trade marked failed (operator can
  cancel manually if the order is still resting on the exchange).
- stop placement exhausted retries → trade marked failed AND
  :class:`NativeStopPlacer` already alerted the admin; the trade is
  in a critical state because the entry filled but no stop is in
  place. The reconciler will catch the orphan stop attempt.

All public methods are async and never raise — failures land in the
``ExecutionResult`` so the caller (Telegram approval handler,
scheduled job) can render a deterministic status without try/except.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from mib.logger import logger
from mib.trading.alerter import NullAlerter, TelegramAlerter
from mib.trading.fill_detector import FillDetector
from mib.trading.order_repo import OrderRepository
from mib.trading.signals import Signal
from mib.trading.stop_placer import NativeStopPlacer
from mib.trading.trade_repo import TradeRepository
from mib.trading.trades import Trade, TradeInputs

if TYPE_CHECKING:  # pragma: no cover
    from mib.sources.ccxt_trader import CCXTTrader
    from mib.trading.risk.decision import RiskDecision


ExecutionStatus = Literal["open", "failed", "skipped"]


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of one ``execute()`` call. Never raises."""

    status: ExecutionStatus
    trade_id: int | None = None
    entry_order_id: int | None = None
    stop_order_id: int | None = None
    filled_amount: Decimal = Decimal(0)
    reason: str | None = None


class OrderExecutor:
    """Sequences entry → fill → stop → trade-open in one transaction-
    of-events. Persistence is per-step (each step is its own DB
    transaction); the trade row is the canonical state machine.
    """

    def __init__(
        self,
        *,
        trader: CCXTTrader,
        order_repo: OrderRepository,
        trade_repo: TradeRepository,
        fill_detector: FillDetector,
        stop_placer: NativeStopPlacer,
        alerter: TelegramAlerter | None = None,
        exchange_id: str = "binance_sandbox",
    ) -> None:
        self._trader = trader
        self._orders = order_repo
        self._trades = trade_repo
        self._fill = fill_detector
        self._stop = stop_placer
        self._alerter = alerter or NullAlerter()
        self._exchange_id = exchange_id

    async def execute(
        self, decision: RiskDecision, signal: Signal
    ) -> ExecutionResult:
        """Run the full open-a-trade flow. Always returns; never raises."""
        try:
            return await self._execute_inner(decision, signal)
        except Exception as exc:  # noqa: BLE001 — defensive surface
            logger.exception(
                "executor: unexpected failure for signal_id={}: {}",
                decision.signal_id,
                exc,
            )
            return ExecutionResult(
                status="failed",
                reason=f"unexpected: {exc.__class__.__name__}: {exc}",
            )

    # ─── Inner flow ────────────────────────────────────────────────

    async def _execute_inner(
        self, decision: RiskDecision, signal: Signal
    ) -> ExecutionResult:
        if not decision.approved:
            return ExecutionResult(
                status="skipped",
                reason="risk_decision.approved=False",
            )
        if decision.sized_amount is None or decision.sized_amount <= 0:
            return ExecutionResult(
                status="skipped",
                reason="sized_amount missing or non-positive",
            )

        entry_price = _entry_price(signal)
        amount_base = _amount_in_base(
            sized_amount_quote=decision.sized_amount, entry_price=entry_price
        )
        if amount_base <= 0:
            return ExecutionResult(
                status="skipped",
                reason="computed amount_base is zero",
            )

        # 1) Trade row.
        try:
            trade = await self._trades.add(
                TradeInputs(
                    signal_id=decision.signal_id,
                    ticker=signal.ticker,
                    side=signal.side,  # type: ignore[arg-type]
                    size=amount_base,
                    entry_price=entry_price,
                    stop_loss_price=Decimal(str(signal.invalidation)),
                    take_profit_price=Decimal(str(signal.target_1)),
                    exchange_id=self._exchange_id,
                    metadata={
                        "strategy_id": signal.strategy_id,
                        "risk_decision_version": decision.version,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor: trade.add failed signal_id={}: {}",
                decision.signal_id,
                exc,
            )
            return ExecutionResult(
                status="failed",
                reason=f"trade.add: {exc.__class__.__name__}: {exc}",
            )

        # 2) Entry order.
        entry_side = "buy" if signal.side == "long" else "sell"
        try:
            entry = await self._trader.create_order(
                signal_id=decision.signal_id,
                symbol=signal.ticker,
                side=entry_side,  # type: ignore[arg-type]
                type="limit",
                amount=amount_base,
                price=entry_price,
                reduce_only=False,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                trade,
                reason=f"create_order: {exc.__class__.__name__}: {exc}",
            )

        if entry.status not in ("submitted", "filled"):
            # cancelled (seatbelt), rejected (4xx), failed (timeout).
            return await self._fail(
                trade,
                reason=f"entry status={entry.status}: {entry.reason or 'n/a'}",
                entry_order_id=entry.order_id,
            )

        # 3) Wait for fill (only when not already filled by the exchange).
        if entry.status == "filled":
            filled_amount = entry.amount
        else:
            fill_result = await self._fill.wait_for_fill(
                entry.order_id, symbol=signal.ticker
            )
            if not fill_result.filled:
                return await self._fail(
                    trade,
                    reason=(
                        f"fill_detector status={fill_result.final_status}: "
                        f"{fill_result.reason or 'n/a'}"
                    ),
                    entry_order_id=entry.order_id,
                )
            filled_amount = fill_result.filled_amount or amount_base

        # 4) Native stop.
        stop_result = await self._stop.place_stop_after_fill(
            signal, entry.order_id, filled_amount=filled_amount
        )
        if not stop_result.success:
            # NativeStopPlacer already alerted the admin; we mark the
            # trade as failed because the entry is filled with no
            # protective stop in place.
            return await self._fail(
                trade,
                reason=(
                    f"stop_placer attempts={stop_result.attempts}: "
                    f"{stop_result.reason or 'n/a'}"
                ),
                entry_order_id=entry.order_id,
                filled_amount=filled_amount,
            )

        # 5) Link both orders to the trade.
        order_ids = [entry.order_id]
        if stop_result.stop_order_id is not None:
            order_ids.append(stop_result.stop_order_id)
        try:
            await self._trades.link_orders_to_trade(trade.trade_id, order_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor: link_orders_to_trade failed trade_id={}: {}",
                trade.trade_id,
                exc,
            )
            # Non-fatal: orders + stop are placed. Operator can patch
            # via reconciler. Continue to mark the trade as open.

        # 6) Transition trade pending → open.
        try:
            await self._trades.transition(
                trade.trade_id,
                "open",
                actor="executor",
                event_type="opened",
                expected_from_status="pending",
                metadata={
                    "entry_order_id": entry.order_id,
                    "stop_order_id": stop_result.stop_order_id,
                    "filled_amount": str(filled_amount),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor: trade transition pending->open failed: {}", exc
            )

        await self._notify_open(trade, signal, filled_amount, entry, stop_result)
        return ExecutionResult(
            status="open",
            trade_id=trade.trade_id,
            entry_order_id=entry.order_id,
            stop_order_id=stop_result.stop_order_id,
            filled_amount=filled_amount,
        )

    async def _fail(
        self,
        trade: Trade,
        *,
        reason: str,
        entry_order_id: int | None = None,
        filled_amount: Decimal = Decimal(0),
    ) -> ExecutionResult:
        """Helper: transition trade → failed + return result."""
        try:
            await self._trades.transition(
                trade.trade_id,
                "failed",
                actor="executor",
                event_type="failed",
                reason=reason,
                metadata={
                    "entry_order_id": entry_order_id,
                    "filled_amount": str(filled_amount),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "executor: failed to mark trade #{} failed: {}",
                trade.trade_id,
                exc,
            )
        try:
            await self._alerter.alert(
                "❌ <b>Trade failed</b>\n"
                f"trade #{trade.trade_id} ticker <code>{trade.ticker}</code>\n"
                f"reason: <code>{reason[:300]}</code>"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("executor: alert on _fail failed: {}", exc)
        return ExecutionResult(
            status="failed",
            trade_id=trade.trade_id,
            entry_order_id=entry_order_id,
            filled_amount=filled_amount,
            reason=reason,
        )

    async def _notify_open(
        self,
        trade: Trade,
        signal: Signal,
        filled_amount: Decimal,
        entry: Any,
        stop_result: Any,
    ) -> None:
        message = (
            "✅ <b>Trade open</b>\n"
            f"trade #{trade.trade_id} ticker <code>{signal.ticker}</code>\n"
            f"side: <code>{signal.side}</code>  size: <code>{filled_amount}</code>\n"
            f"entry order #{entry.order_id} @ <code>{trade.entry_price}</code>\n"
            f"stop order #{stop_result.stop_order_id} "
            f"(attempts={stop_result.attempts})"
        )
        try:
            await self._alerter.alert(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("executor: notify_open alert failed: {}", exc)


# ─── Pure helpers ────────────────────────────────────────────────────


def _entry_price(signal: Signal) -> Decimal:
    """Limit-order price = midpoint of the entry zone.

    Using the midpoint keeps the order inside the operator's
    confidence band; using the lower edge for longs/upper edge for
    shorts can be added later as a tuning lever, but the midpoint is
    a sensible default that doesn't try to outsmart the strategy.
    """
    low, high = signal.entry_zone
    return (Decimal(str(low)) + Decimal(str(high))) / Decimal(2)


def _amount_in_base(
    *, sized_amount_quote: Decimal, entry_price: Decimal
) -> Decimal:
    """Convert the risk-sized EUR amount into base-currency units.

    The result is rounded to 8 decimals (Binance's spot lot precision
    cap). Exchange-side rounding will further quantise per market.
    """
    if entry_price <= 0:
        return Decimal(0)
    raw = sized_amount_quote / entry_price
    # Quantise to 8 decimals — the lot-step rounding lives at the
    # exchange / FASE 14 instrument-info layer.
    return raw.quantize(Decimal("0.00000001"))
