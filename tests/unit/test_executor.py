"""Tests for :class:`OrderExecutor` (FASE 9.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mib.db.session import async_session_factory
from mib.sources.ccxt_trader import CCXTTrader
from mib.trading.alerter import NullAlerter
from mib.trading.executor import (
    OrderExecutor,
    _amount_in_base,
    _entry_price,
)
from mib.trading.fill_detector import FillDetector, FillResult
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderResult
from mib.trading.risk.decision import RiskDecision
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.stop_placer import NativeStopPlacer, StopPlacementResult
from mib.trading.trade_repo import TradeRepository

# ─── Fakes ────────────────────────────────────────────────────────────


class _FakeTrader(CCXTTrader):
    """Minimal trader stub: records create_order calls + returns canned
    :class:`OrderResult`. Skips the real exchange completely.
    """

    def __init__(
        self,
        order_repo: OrderRepository,
        *,
        next_results: list[OrderResult] | None = None,
        raise_on: int | None = None,
    ) -> None:
        super().__init__(
            exchange_id="binance",
            api_key="fake",
            api_secret="fake",
            base_url="https://testnet.binance.vision",
            dry_run=False,
            order_repo=order_repo,
        )
        self._scripted: list[OrderResult] = list(next_results or [])
        self._raise_on = raise_on
        self.calls: list[dict[str, Any]] = []

    async def create_order(  # type: ignore[override]
        self,
        *,
        signal_id: int,
        symbol: str,
        side: Any,
        type: Any,  # noqa: A002
        amount: Decimal,
        price: Decimal | None = None,
        reduce_only: bool = False,
        extra_params: dict[str, Any] | None = None,
    ) -> OrderResult:
        self.calls.append(
            {
                "signal_id": signal_id,
                "symbol": symbol,
                "side": side,
                "type": type,
                "amount": amount,
                "price": price,
                "reduce_only": reduce_only,
                "extra_params": extra_params or {},
            }
        )
        if self._raise_on is not None and len(self.calls) == self._raise_on:
            raise RuntimeError("simulated exchange failure")
        if not self._scripted:
            raise AssertionError("FakeTrader: no scripted result for call")
        return self._scripted.pop(0)


class _StubFillDetector(FillDetector):
    """Returns a canned FillResult without polling."""

    def __init__(self, result: FillResult) -> None:
        self._result = result

    async def wait_for_fill(  # type: ignore[override]
        self, order_db_id: int, *, symbol: str | None = None  # noqa: ARG002
    ) -> FillResult:
        return self._result


class _StubStopPlacer(NativeStopPlacer):
    """Returns a canned StopPlacementResult."""

    def __init__(self, result: StopPlacementResult) -> None:
        self._result = result

    async def place_stop_after_fill(  # type: ignore[override]
        self,
        signal: Signal,  # noqa: ARG002
        entry_order_id: int,  # noqa: ARG002
        *,
        filled_amount: Decimal | None = None,  # noqa: ARG002
    ) -> StopPlacementResult:
        return self._result


# ─── Helpers ──────────────────────────────────────────────────────────


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_100.0),
        invalidation=58_800.0,
        target_1=63_000.0,
        target_2=66_000.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _decision(
    signal_id: int,
    *,
    approved: bool = True,
    sized: Decimal | None = Decimal("60.05"),
) -> RiskDecision:
    return RiskDecision(
        signal_id=signal_id,
        version=1,
        approved=approved,
        gate_results=(),
        reasoning="test",
        decided_at=datetime.now(UTC),
        sized_amount=sized,
    )


def _ok_order_result(
    *, order_id: int, status: str = "submitted", signal_id: int = 1
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        signal_id=signal_id,
        client_order_id=f"mib-{signal_id}-aaaa",
        exchange_order_id=f"exch-{order_id}",
        status=status,  # type: ignore[arg-type]
        side="buy",
        type="limit",
        amount=Decimal("0.001"),
        price=Decimal("60050"),
        reason=None,
        raw_response_json=None,
        decided_at=None,
    )


async def _seed_signal() -> int:
    sr = SignalRepository(async_session_factory)
    p = await sr.add(_signal())
    return p.id


def _build_executor(
    trader: CCXTTrader,
    fill_result: FillResult,
    stop_result: StopPlacementResult,
    alerter: NullAlerter | None = None,
) -> tuple[OrderExecutor, NullAlerter]:
    order_repo = OrderRepository(async_session_factory)
    trade_repo = TradeRepository(async_session_factory)
    used_alerter = alerter or NullAlerter()
    return (
        OrderExecutor(
            trader=trader,
            order_repo=order_repo,
            trade_repo=trade_repo,
            fill_detector=_StubFillDetector(fill_result),
            stop_placer=_StubStopPlacer(stop_result),
            alerter=used_alerter,
            exchange_id="binance_sandbox",
        ),
        used_alerter,
    )


# ─── Pure helpers ─────────────────────────────────────────────────────


def test_entry_price_midpoint() -> None:
    sig = _signal()
    assert _entry_price(sig) == Decimal("60050")


def test_amount_in_base() -> None:
    amount = _amount_in_base(
        sized_amount_quote=Decimal("60.05"),
        entry_price=Decimal("60050"),
    )
    # 60.05 / 60050 = 0.001 exactly.
    assert amount == Decimal("0.00100000")


def test_amount_in_base_zero_price() -> None:
    assert _amount_in_base(
        sized_amount_quote=Decimal("60"), entry_price=Decimal(0)
    ) == Decimal(0)


# ─── Skip paths ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_skipped_when_decision_not_approved(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trader = _FakeTrader(OrderRepository(async_session_factory))
    executor, _ = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=True,
            stop_order_id=2,
            exchange_order_id="exch-2",
            attempts=1,
        ),
    )
    result = await executor.execute(_decision(sid, approved=False), _signal())
    assert result.status == "skipped"
    assert "approved=False" in (result.reason or "")
    assert trader.calls == []


@pytest.mark.asyncio
async def test_execute_skipped_when_no_size(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trader = _FakeTrader(OrderRepository(async_session_factory))
    executor, _ = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=True, stop_order_id=2, exchange_order_id="exch-2", attempts=1
        ),
    )
    result = await executor.execute(_decision(sid, sized=None), _signal())
    assert result.status == "skipped"


# ─── Happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_full_open_flow(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[_ok_order_result(order_id=1, signal_id=sid)],
    )
    executor, alerter = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=True, stop_order_id=2, exchange_order_id="exch-2", attempts=1
        ),
    )
    result = await executor.execute(_decision(sid), _signal())

    assert result.status == "open"
    assert result.trade_id is not None
    assert result.entry_order_id == 1
    assert result.stop_order_id == 2
    assert result.filled_amount == Decimal("0.001")
    assert len(trader.calls) == 1
    call = trader.calls[0]
    assert call["symbol"] == "BTC/USDT"
    assert call["side"] == "buy"
    assert call["type"] == "limit"
    # Trade is now 'open' in DB.
    trade_repo = TradeRepository(async_session_factory)
    trade = await trade_repo.get(result.trade_id)
    assert trade is not None
    assert trade.status == "open"
    # An open alert was sent.
    assert any("Trade open" in t for t in alerter.recorded)


# ─── Failure paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_fails_when_entry_seatbelt_blocks(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """If trader returns 'cancelled' (seatbelt), trade is marked failed."""
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[
            OrderResult(
                order_id=1,
                signal_id=sid,
                client_order_id=f"mib-{sid}-zzzz",
                exchange_order_id=None,
                status="cancelled",
                side="buy",
                type="limit",
                amount=Decimal("0.001"),
                price=Decimal("60050"),
                reason="blocked by triple seatbelt",
                raw_response_json=None,
                decided_at=None,
            )
        ],
    )
    executor, alerter = _build_executor(
        trader,
        FillResult(filled=False, filled_amount=Decimal(0), final_status="cancelled"),
        StopPlacementResult(
            success=False, stop_order_id=None, exchange_order_id=None, attempts=0
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.status == "failed"
    assert "cancelled" in (result.reason or "")
    trade_repo = TradeRepository(async_session_factory)
    trade = await trade_repo.get(result.trade_id) if result.trade_id else None
    assert trade is not None
    assert trade.status == "failed"
    assert trade.closed_at is not None
    assert any("Trade failed" in t for t in alerter.recorded)


@pytest.mark.asyncio
async def test_execute_fails_on_fill_timeout(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[_ok_order_result(order_id=1, signal_id=sid)],
    )
    executor, _ = _build_executor(
        trader,
        FillResult(
            filled=False,
            filled_amount=Decimal(0),
            final_status="timeout",
            reason="polling timed out after 30s",
        ),
        StopPlacementResult(
            success=True, stop_order_id=2, exchange_order_id="exch-2", attempts=1
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.status == "failed"
    assert "timeout" in (result.reason or "")


@pytest.mark.asyncio
async def test_execute_fails_when_stop_placement_fails(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[_ok_order_result(order_id=1, signal_id=sid)],
    )
    executor, alerter = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=False,
            stop_order_id=None,
            exchange_order_id=None,
            attempts=3,
            reason="exchange 5xx after 3 retries",
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.status == "failed"
    assert "stop_placer" in (result.reason or "")
    assert any("Trade failed" in t for t in alerter.recorded)


@pytest.mark.asyncio
async def test_execute_handles_trader_exception(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Trader raising during create_order → trade still gets marked failed."""
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[],
        raise_on=1,
    )
    executor, _ = _build_executor(
        trader,
        FillResult(filled=False, filled_amount=Decimal(0), final_status="failed"),
        StopPlacementResult(
            success=False, stop_order_id=None, exchange_order_id=None, attempts=0
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.status == "failed"
    assert "create_order" in (result.reason or "")


@pytest.mark.asyncio
async def test_execute_links_orders_to_trade(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """After success, both entry + stop carry the FK to trades.id."""
    from mib.db.models import OrderRow  # noqa: PLC0415

    sid = await _seed_signal()
    repo = OrderRepository(async_session_factory)
    # Pre-seed the stop order so order_id=2 actually exists in DB.
    # We simulate this by routing the trader's "stop" call through the
    # real repo. For this test we hand-make a row instead.
    trader = _FakeTrader(
        repo,
        next_results=[_ok_order_result(order_id=1, signal_id=sid)],
    )

    # Hand-create order_id=2 as the stop. ``link_orders_to_trade``
    # validates the FK so the row needs to exist before linking.
    from mib.trading.orders import OrderInputs  # noqa: PLC0415

    stop_inputs = OrderInputs(
        signal_id=sid,
        symbol="BTC/USDT",
        side="sell",
        type="stop_market",
        amount=Decimal("0.001"),
        price=Decimal("58800"),
        reduce_only=True,
    )
    stop_row = await repo.add_or_get(
        stop_inputs,
        exchange_id="binance_sandbox",
        raw_payload={"symbol": "BTC/USDT"},
    )
    # Likewise hand-create order_id=1 as the entry so the FK link can
    # find it (the FakeTrader returns synthetic order_id=1 without
    # actually persisting it).
    entry_inputs = OrderInputs(
        signal_id=sid,
        symbol="BTC/USDT",
        side="buy",
        type="limit",
        amount=Decimal("0.001"),
        price=Decimal("60050"),
    )
    entry_row = await repo.add_or_get(
        entry_inputs,
        exchange_id="binance_sandbox",
        raw_payload={"symbol": "BTC/USDT"},
    )

    # Patch the scripted result to use the actual DB ids.
    trader._scripted = [  # noqa: SLF001
        OrderResult(
            order_id=entry_row.order_id,
            signal_id=sid,
            client_order_id=entry_row.client_order_id,
            exchange_order_id="exch-1",
            status="submitted",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60050"),
            reason=None,
            raw_response_json=None,
            decided_at=None,
        )
    ]

    executor, _ = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=True,
            stop_order_id=stop_row.order_id,
            exchange_order_id="exch-stop",
            attempts=1,
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.status == "open"

    async with async_session_factory() as session:
        for oid in (entry_row.order_id, stop_row.order_id):
            row = await session.get(OrderRow, oid)
            assert row is not None
            assert row.trade_id == result.trade_id


@pytest.mark.asyncio
async def test_execute_writes_trade_events(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Trade lifecycle is fully audit-logged: created → opened."""
    sid = await _seed_signal()
    trader = _FakeTrader(
        OrderRepository(async_session_factory),
        next_results=[_ok_order_result(order_id=1, signal_id=sid)],
    )
    executor, _ = _build_executor(
        trader,
        FillResult(filled=True, filled_amount=Decimal("0.001"), final_status="filled"),
        StopPlacementResult(
            success=True, stop_order_id=2, exchange_order_id="exch-2", attempts=1
        ),
    )
    result = await executor.execute(_decision(sid), _signal())
    assert result.trade_id is not None

    trade_repo = TradeRepository(async_session_factory)
    events = await trade_repo.list_events(result.trade_id)
    assert [e.event_type for e in events] == ["created", "opened"]
    assert events[0].from_status is None
    assert events[1].from_status == "pending"
    assert events[1].to_status == "open"
