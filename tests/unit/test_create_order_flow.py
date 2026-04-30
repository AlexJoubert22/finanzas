"""End-to-end flow tests for ``CCXTTrader.create_order`` (FASE 9.2).

These cover the contract beyond the seatbelt + repo unit slices:

- Triple seatbelt blocks the exchange call but still persists the
  audit row (status='cancelled' with reason).
- Real path: exchange ack → status='submitted' with exchange_order_id.
- Exchange 4xx → status='rejected' with reason captured.
- Exchange timeout → status='failed' (network bucket).
- Idempotent retry: same inputs → same row, no second exchange call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.sources.ccxt_trader import CCXTTrader
from mib.trading.order_repo import OrderRepository
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal() -> Signal:
    from datetime import UTC, datetime  # noqa: PLC0415

    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_000.0),
        invalidation=58_800.0,
        target_1=61_200.0,
        target_2=63_600.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


async def _seed_signal() -> int:
    sr = SignalRepository(async_session_factory)
    p = await sr.add(_signal())
    return p.id


def _make_trader(*, dry_run: bool, is_sandbox: bool = True) -> CCXTTrader:
    repo = OrderRepository(async_session_factory)
    base_url = "https://testnet.binance.vision" if is_sandbox else "https://api.binance.com"
    return CCXTTrader(
        api_key="k",
        api_secret="s",
        base_url=base_url,
        dry_run=dry_run,
        order_repo=repo,
    )


class _StubExchange:
    """Records calls and returns canned responses."""

    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._response = response

    async def create_order(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ─── Triple seatbelt path (no exchange call) ─────────────────────────

@pytest.mark.asyncio
async def test_dry_run_persists_row_and_marks_cancelled(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sid = await _seed_signal()
    t = _make_trader(dry_run=True)
    result = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    assert result.status == "cancelled"
    assert "triple seatbelt" in (result.reason or "").lower()
    # Audit trail captured the gate decision.
    repo = OrderRepository(async_session_factory)
    events = await repo.list_events(result.order_id)
    assert [e.event_type for e in events] == ["created", "cancelled"]
    assert events[1].actor == "ccxt-trader:gate"


# ─── Real path (all gates open) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_open_gates_path_submits_and_marks_submitted(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await _seed_signal()
    monkeypatch.setattr(get_settings(), "trading_enabled", True, raising=False)

    t = _make_trader(dry_run=False)
    stub = _StubExchange({"id": "exch-99", "status": "open"})
    monkeypatch.setattr(t, "_ensure_exchange", _make_async_returning(stub))

    result = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    assert result.status == "submitted"
    assert result.exchange_order_id == "exch-99"
    # newClientOrderId param threaded through
    assert len(stub.calls) == 1
    call_args = stub.calls[0]
    assert call_args[0] == "BTC/USDT"
    assert call_args[1] == "limit"
    assert call_args[2] == "buy"
    params = call_args[5]
    assert params["newClientOrderId"] == result.client_order_id


# ─── Failure paths ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exchange_4xx_marks_rejected(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await _seed_signal()
    monkeypatch.setattr(get_settings(), "trading_enabled", True, raising=False)

    class InsufficientBalanceError(Exception):
        pass

    # Drop the canonical "Error" suffix the lint expects so the exception
    # *name* surfaced to the user matches what ccxt would raise upstream.
    InsufficientBalanceError.__name__ = "InsufficientBalance"
    t = _make_trader(dry_run=False)
    stub = _StubExchange(InsufficientBalanceError("balance too low"))
    monkeypatch.setattr(t, "_ensure_exchange", _make_async_returning(stub))

    result = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    assert result.status == "rejected"
    assert "InsufficientBalance" in (result.reason or "")


@pytest.mark.asyncio
async def test_exchange_timeout_marks_failed(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await _seed_signal()
    monkeypatch.setattr(get_settings(), "trading_enabled", True, raising=False)

    class RequestTimeoutError(Exception):
        pass

    RequestTimeoutError.__name__ = "RequestTimeout"
    t = _make_trader(dry_run=False)
    stub = _StubExchange(RequestTimeoutError("exchange timed out"))
    monkeypatch.setattr(t, "_ensure_exchange", _make_async_returning(stub))

    result = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    assert result.status == "failed"
    assert "RequestTimeout" in (result.reason or "")


# ─── Idempotency ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idempotent_retry_returns_existing_no_new_exchange_call(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await _seed_signal()
    monkeypatch.setattr(get_settings(), "trading_enabled", True, raising=False)

    t = _make_trader(dry_run=False)
    stub = _StubExchange({"id": "exch-77", "status": "open"})
    monkeypatch.setattr(t, "_ensure_exchange", _make_async_returning(stub))

    first = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    second = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    assert first.order_id == second.order_id
    assert first.client_order_id == second.client_order_id
    assert second.status == "submitted"
    # Exchange called exactly once.
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_different_amount_creates_new_row(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = await _seed_signal()
    monkeypatch.setattr(get_settings(), "trading_enabled", True, raising=False)

    t = _make_trader(dry_run=False)
    stub = _StubExchange({"id": "exch-x", "status": "open"})
    monkeypatch.setattr(t, "_ensure_exchange", _make_async_returning(stub))

    a = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.001"), price=Decimal("60000"),
    )
    b = await t.create_order(
        signal_id=sid, symbol="BTC/USDT", side="buy", type="limit",
        amount=Decimal("0.002"), price=Decimal("60000"),
    )
    assert a.order_id != b.order_id
    assert a.client_order_id != b.client_order_id


# ─── Helpers ────────────────────────────────────────────────────────

def _make_async_returning(value: Any) -> Any:
    """Build an async callable that returns ``value`` regardless of args."""

    async def _factory(*_a: Any, **_kw: Any) -> Any:
        return value

    return _factory
