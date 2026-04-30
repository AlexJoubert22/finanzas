"""Tests for :class:`FillDetector`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mib.db.session import async_session_factory
from mib.trading.fill_detector import FillDetector
from mib.trading.order_repo import OrderRepository
from mib.trading.orders import OrderInputs
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal() -> Signal:
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


async def _seed_submitted_order() -> int:
    """Persist signal + order in 'submitted' status."""
    sr = SignalRepository(async_session_factory)
    persisted = await sr.add(_signal())
    repo = OrderRepository(async_session_factory)
    o = await repo.add_or_get(
        OrderInputs(
            signal_id=persisted.id,
            symbol="BTC/USDT",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )
    await repo.transition(
        o.order_id,
        "submitted",
        actor="test",
        event_type="submitted",
        exchange_order_id="exch-fill-1",
    )
    return o.order_id


class _StubTrader:
    """Returns canned ``fetch_order`` responses in sequence."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def fetch_order(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"symbol": symbol, **kwargs})
        if not self._responses:
            return {"status": "open"}  # default keeps polling
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_already_terminal_returns_immediately(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sr = SignalRepository(async_session_factory)
    persisted = await sr.add(_signal())
    repo = OrderRepository(async_session_factory)
    o = await repo.add_or_get(
        OrderInputs(
            signal_id=persisted.id,
            symbol="BTC/USDT",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )
    await repo.transition(
        o.order_id, "submitted", actor="test", event_type="submitted"
    )
    await repo.transition(
        o.order_id, "filled", actor="test", event_type="filled"
    )
    trader = _StubTrader([])
    detector = FillDetector(
        trader=trader,  # type: ignore[arg-type]
        order_repo=repo,
        timeout_seconds=1.0,
    )
    result = await detector.wait_for_fill(o.order_id)
    assert result.filled is True
    assert result.final_status == "filled"
    assert len(trader.calls) == 0


@pytest.mark.asyncio
async def test_fill_after_one_poll(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
    order_id = await _seed_submitted_order()
    trader = _StubTrader(
        [{"status": "closed", "filled": "0.001", "id": "exch-fill-1"}]
    )
    detector = FillDetector(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        timeout_seconds=10.0,
    )
    result = await detector.wait_for_fill(order_id)
    assert result.filled is True
    assert result.filled_amount == Decimal("0.001")


@pytest.mark.asyncio
async def test_cancelled_terminates_polling(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
    order_id = await _seed_submitted_order()
    trader = _StubTrader(
        [{"status": "canceled", "filled": "0", "id": "exch-fill-1"}]
    )
    detector = FillDetector(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        timeout_seconds=10.0,
    )
    result = await detector.wait_for_fill(order_id)
    assert result.filled is False
    assert result.final_status == "cancelled"


@pytest.mark.asyncio
async def test_timeout_returns_with_timeout_status(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling deadline elapses while exchange keeps returning 'open'."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
    order_id = await _seed_submitted_order()
    trader = _StubTrader(
        [
            {"status": "open"},
            {"status": "open"},
            {"status": "open"},
            {"status": "open"},
            {"status": "open"},
        ]
    )
    # Aggressive timeout: by setting a tiny window, the loop runs once-or-twice
    # and exits via the deadline check.
    detector = FillDetector(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        timeout_seconds=0.0001,
        poll_interval_seconds=0.0,
    )
    result = await detector.wait_for_fill(order_id)
    assert result.filled is False
    assert result.final_status == "timeout"


@pytest.mark.asyncio
async def test_fetch_order_exception_is_retried(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient fetch_order failure shouldn't end the loop early."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
    order_id = await _seed_submitted_order()

    class _FlakyTrader:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch_order(self, *_args: Any, **_kw: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return {"status": "closed", "filled": "0.001", "id": "exch-fill-1"}

    trader = _FlakyTrader()
    detector = FillDetector(
        trader=trader,  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        timeout_seconds=10.0,
    )
    result = await detector.wait_for_fill(order_id)
    assert result.filled is True
    assert trader.calls == 2


@pytest.mark.asyncio
async def test_unknown_order_id_returns_not_found(
    fresh_db: None,  # noqa: ARG001
) -> None:
    detector = FillDetector(
        trader=_StubTrader([]),  # type: ignore[arg-type]
        order_repo=OrderRepository(async_session_factory),
        timeout_seconds=1.0,
    )
    result = await detector.wait_for_fill(9999)
    assert result.filled is False
    assert result.final_status == "not_found"


@pytest.mark.asyncio
async def test_no_exchange_order_id_returns_clean_failure(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Order in 'created' (never reached exchange) — fill is impossible."""
    sr = SignalRepository(async_session_factory)
    persisted = await sr.add(_signal())
    repo = OrderRepository(async_session_factory)
    o = await repo.add_or_get(
        OrderInputs(
            signal_id=persisted.id,
            symbol="BTC/USDT",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
        ),
        exchange_id="binance_sandbox",
        raw_payload={},
    )
    detector = FillDetector(
        trader=_StubTrader([]),  # type: ignore[arg-type]
        order_repo=repo,
        timeout_seconds=1.0,
    )
    result = await detector.wait_for_fill(o.order_id)
    assert result.filled is False
    assert "no exchange_order_id" in (result.reason or "")
