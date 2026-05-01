"""Panic action (FASE 13.6) — cancel-all + close-all + 7-day kill window.

The /panic Telegram command calls :func:`execute_panic`, which:

1. Cancels every still-open exchange order it can find.
2. Closes every open trade with a market order in the opposite side
   (``reduce_only=True``).
3. Flips ``trading_state.enabled`` to False.
4. Sets ``trading_state.killed_until = next_utc_midnight + 7 days``
   so the kill window is prolonged (vs the daily-DD gate's 1-day kill
   for normal threshold breaches).

Target latency end-to-end: <3 seconds in the smoke test against the
sandbox. The implementation is **defensive about partial failures**:
each cancel / close is wrapped in a try/except so one bad symbol
doesn't abort the entire panic. The kill switch flip happens AFTER
the per-order steps, so even a fully botched cancel run still ends
with new signals blocked.

The function returns a :class:`PanicReport` so the Telegram handler
(and tests) can render exactly what landed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from mib.sources.ccxt_trader import CCXTTrader
    from mib.trading.order_repo import OrderRepository
    from mib.trading.risk.state import TradingStateService
    from mib.trading.trade_repo import TradeRepository

#: Length of the prolonged kill window after /panic.
PANIC_KILL_WINDOW_DAYS: int = 7


@dataclass(frozen=True)
class PanicReport:
    """Outcome of one /panic invocation."""

    actor: str
    started_at: datetime
    finished_at: datetime
    cancelled_orders: list[dict[str, Any]] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    killed_until: datetime | None = None

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled_orders)

    @property
    def closed_count(self) -> int:
        return len(self.closed_trades)


async def execute_panic(
    *,
    actor: str,
    trader: CCXTTrader,
    order_repo: OrderRepository,
    trade_repo: TradeRepository,
    state_service: TradingStateService,
) -> PanicReport:
    """Carry out the panic flow. Never raises — every step is guarded.

    Steps run in fixed order: cancel → close → kill switch. Each step
    is best-effort; failures are captured in ``report.errors`` for
    operator visibility.
    """
    started = datetime.now(UTC).replace(tzinfo=None)
    cancelled: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    errors: list[str] = []

    # 1) Cancel all open orders persisted in our DB.
    try:
        open_orders = await order_repo.list_open_by_status()
    except Exception as exc:  # noqa: BLE001
        msg = f"list_open_by_status failed: {exc}"
        errors.append(msg)
        logger.error("panic: {}", msg)
        open_orders = []

    for o in open_orders:
        symbol = _symbol_for(o)
        try:
            await trader.cancel_order(
                symbol,
                exchange_order_id=o.exchange_order_id,
                client_order_id=o.client_order_id,
            )
            cancelled.append({
                "order_id": o.order_id,
                "symbol": symbol,
                "client_order_id": o.client_order_id,
            })
        except Exception as exc:  # noqa: BLE001
            msg = (
                f"cancel order_id={o.order_id} ({symbol}) failed: {exc}"
            )
            errors.append(msg)
            logger.warning("panic: {}", msg)

    # 2) Close all open trades.
    try:
        open_trades = await trade_repo.list_open()
    except Exception as exc:  # noqa: BLE001
        msg = f"list_open trades failed: {exc}"
        errors.append(msg)
        logger.error("panic: {}", msg)
        open_trades = []

    for t in open_trades:
        opposite = "sell" if t.side == "long" else "buy"
        try:
            await trader.close_position(
                t.ticker,
                opposite,
                float(t.size),
            )
            closed.append({
                "trade_id": t.trade_id,
                "ticker": t.ticker,
                "side": opposite,
                "size": str(t.size),
            })
        except Exception as exc:  # noqa: BLE001
            msg = (
                f"close trade_id={t.trade_id} ({t.ticker}) failed: {exc}"
            )
            errors.append(msg)
            logger.warning("panic: {}", msg)

    # 3) Flip the kill switch with the prolonged window.
    killed_until: datetime | None = None
    try:
        midnight = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        killed_until = (
            midnight + timedelta(days=PANIC_KILL_WINDOW_DAYS - 1)
        ).replace(tzinfo=None)
        await state_service.update(
            actor=f"panic:{actor}",
            enabled=False,
            killed_until=killed_until,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"trading_state update failed: {exc}"
        errors.append(msg)
        logger.error("panic: {}", msg)

    finished = datetime.now(UTC).replace(tzinfo=None)
    elapsed_ms = int((finished - started).total_seconds() * 1000)
    logger.warning(
        "panic: actor={} cancelled={} closed={} errors={} elapsed_ms={}",
        actor,
        len(cancelled),
        len(closed),
        len(errors),
        elapsed_ms,
    )
    # Performance budget: log warn over 3s threshold so the smoke
    # test catches regressions even when the assertion lives in the
    # caller.
    if elapsed_ms > 3000:
        logger.warning(
            "panic: latency budget exceeded (>3s) elapsed_ms={}", elapsed_ms
        )

    return PanicReport(
        actor=actor,
        started_at=started,
        finished_at=finished,
        cancelled_orders=cancelled,
        closed_trades=closed,
        errors=errors,
        killed_until=killed_until,
    )


def _symbol_for(order: Any) -> str:
    """Best-effort extraction. Order rows store the symbol in the
    raw_payload_json blob from FASE 9.2."""
    payload = getattr(order, "raw_response_json", None) or {}
    sym = payload.get("symbol") if isinstance(payload, dict) else None
    if isinstance(sym, str):
        return sym
    # Last-resort default — better than empty so cancel_order has a
    # workable arg.
    return "BTC/USDT"
