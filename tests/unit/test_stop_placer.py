"""Tests for :class:`NativeStopPlacer` (FASE 9.3).

Covers:

- Stop placed correctly after fill (long → sell, short → buy)
- ``filled_amount`` overrides ``entry.amount`` when supplied (partial fills)
- Retry mechanics: 1st+2nd attempts transient-fail, 3rd succeeds → no alert
- Permanent error (rejected) → no retry, alert + return failure
- Retries exhausted → alert + StopPlacementResult.success=False
- Entry not in fillable status → immediate failure, no exchange call
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mib.db.session import async_session_factory
from mib.trading.alerter import NullAlerter
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderResult
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal
from mib.trading.stop_placer import NativeStopPlacer


def _signal(side: str = "long") -> Signal:
    if side == "long":
        return Signal(
            ticker="BTC/USDT",
            side="long",
            strength=0.7,
            timeframe="1h",
            entry_zone=(60_000.0, 60_000.0),
            invalidation=58_800.0,
            target_1=61_200.0,
            target_2=63_600.0,
            rationale="t",
            indicators={"rsi_14": 22.0, "atr_14": 800.0},
            generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            strategy_id="scanner.oversold.v1",
            confidence_ai=None,
        )
    return Signal(
        ticker="BTC/USDT",
        side="short",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=61_200.0,
        target_1=58_800.0,
        target_2=56_400.0,
        rationale="t",
        indicators={"rsi_14": 75.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.breakout.v1",
        confidence_ai=None,
    )


async def _seed_signal_and_filled_entry(
    sig: Signal,
) -> tuple[int, int]:
    """Persist a signal and a filled entry order; return (signal_id, entry_order_id)."""
    sr = SignalRepository(async_session_factory)
    persisted = await sr.add(sig)
    repo = OrderRepository(async_session_factory)
    from mib.trading.orders import OrderInputs  # noqa: PLC0415

    inputs = OrderInputs(
        signal_id=persisted.id,
        symbol=sig.ticker,
        side="buy" if sig.side == "long" else "sell",
        type="limit",
        amount=Decimal("0.001"),
        price=Decimal("60000"),
    )
    entry = await repo.add_or_get(
        inputs, exchange_id="binance_sandbox", raw_payload={}
    )
    await repo.transition(
        entry.order_id,
        "submitted",
        actor="test", event_type="submitted",
        exchange_order_id="exch-entry-1",
    )
    await repo.transition(
        entry.order_id, "filled", actor="test", event_type="filled"
    )
    return persisted.id, entry.order_id


class _StubTrader:
    """Records create_order calls and returns a sequence of OrderResults."""

    def __init__(self, results: list[OrderResult | Exception]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def create_order(self, **kwargs: Any) -> OrderResult:
        self.calls.append(kwargs)
        if not self._results:
            raise RuntimeError("no more stub results")
        next_value = self._results.pop(0)
        if isinstance(next_value, Exception):
            raise next_value
        return next_value


def _stop_result(
    *,
    order_id: int = 100,
    status: str = "submitted",
    reason: str | None = None,
) -> OrderResult:
    return OrderResult(
        order_id=order_id,
        signal_id=1,
        client_order_id=f"mib-1-stop-{order_id}",
        exchange_order_id=f"exch-stop-{order_id}",
        status=status,  # type: ignore[arg-type]
        side="sell",
        type="stop_market",
        amount=Decimal("0.001"),
        price=None,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_stop_placed_after_fill_long_signal(fresh_db: None) -> None:  # noqa: ARG001
    """Long signal → stop side is 'sell'."""
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)

    trader = _StubTrader([_stop_result(order_id=200)])
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=NullAlerter(),
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is True
    assert result.stop_order_id == 200
    assert result.attempts == 1
    assert len(trader.calls) == 1
    call = trader.calls[0]
    assert call["side"] == "sell"
    assert call["type"] == "stop_market"
    assert call["reduce_only"] is True
    assert call["extra_params"]["stopPrice"] == "58800.0"


@pytest.mark.asyncio
async def test_stop_placed_after_fill_short_signal(fresh_db: None) -> None:  # noqa: ARG001
    """Short signal → stop side is 'buy'."""
    sig = _signal("short")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)

    trader = _StubTrader([_stop_result(order_id=201)])
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=NullAlerter(),
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is True
    call = trader.calls[0]
    assert call["side"] == "buy"
    assert call["extra_params"]["stopPrice"] == "61200.0"


@pytest.mark.asyncio
async def test_uses_filled_amount_override(fresh_db: None) -> None:  # noqa: ARG001
    """Partial fills: the placer accepts an explicit filled_amount."""
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)
    trader = _StubTrader([_stop_result(order_id=202)])
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=NullAlerter(),
    )
    await placer.place_stop_after_fill(
        sig, entry_id, filled_amount=Decimal("0.0006")
    )
    assert trader.calls[0]["amount"] == Decimal("0.0006")


@pytest.mark.asyncio
async def test_retry_on_transient_failure_eventually_succeeds(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1st attempt failed (transient) → backoff → 2nd success. No alert."""
    monkeypatch.setattr(
        "asyncio.sleep", AsyncMock(return_value=None)  # skip real sleeps
    )
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)

    failed = _stop_result(order_id=300, status="failed", reason="timeout")
    success = _stop_result(order_id=301, status="submitted")
    trader = _StubTrader([failed, success])
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is True
    assert result.attempts == 2
    assert alerter.recorded == []  # no alert fired


@pytest.mark.asyncio
async def test_permanent_rejection_no_retry_alerts(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """``rejected`` is a 4xx-shape: don't waste retries; alert."""
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)
    trader = _StubTrader(
        [_stop_result(order_id=400, status="rejected", reason="invalid_params")]
    )
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is False
    assert "invalid_params" in (result.reason or "")
    assert len(trader.calls) == 1  # no retry
    assert len(alerter.recorded) == 1
    assert "STOP NO COLOCADO" in alerter.recorded[0]


@pytest.mark.asyncio
async def test_three_retries_exhausted_alerts(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 3 transient → alert + success=False."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)
    failed = lambda i: _stop_result(  # noqa: E731
        order_id=500 + i, status="failed", reason=f"timeout-{i}"
    )
    trader = _StubTrader([failed(1), failed(2), failed(3)])
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is False
    assert result.attempts == 3
    assert len(alerter.recorded) == 1
    assert "STOP NO COLOCADO" in alerter.recorded[0]


@pytest.mark.asyncio
async def test_entry_not_filled_returns_immediately(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Caller mistake: invokes placer with an entry still 'submitted'."""
    sig = _signal("long")
    sr = SignalRepository(async_session_factory)
    persisted = await sr.add(sig)
    repo = OrderRepository(async_session_factory)
    from mib.trading.orders import OrderInputs  # noqa: PLC0415

    entry = await repo.add_or_get(
        OrderInputs(
            signal_id=persisted.id,
            symbol=sig.ticker,
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )
    # Leave it in 'created' status — not filled.
    trader = _StubTrader([])
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=repo,
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry.order_id)
    assert result.success is False
    assert "not filled" in (result.reason or "")
    assert len(trader.calls) == 0
    assert alerter.recorded == []  # no alert for caller-side error


@pytest.mark.asyncio
async def test_entry_unknown_id_returns_immediately(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sig = _signal("long")
    trader = _StubTrader([])
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry_order_id=9999)
    assert result.success is False
    assert "not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_seatbelt_block_no_retry(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """``cancelled`` from create_order means triple-seatbelt blocked.
    Retrying won't unblock — exit early without alert spam.
    """
    sig = _signal("long")
    _signal_id, entry_id = await _seed_signal_and_filled_entry(sig)
    blocked = replace(
        _stop_result(order_id=600, status="cancelled"),
        reason="blocked by triple seatbelt",
    )
    trader = _StubTrader([blocked])
    alerter = NullAlerter()
    placer = NativeStopPlacer(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        alerter=alerter,
    )
    result = await placer.place_stop_after_fill(sig, entry_id)
    assert result.success is False
    assert len(trader.calls) == 1  # no retry
    # Triple-seatbelt block IS alerted (operator should know stop wasn't placed).
    assert len(alerter.recorded) == 1
