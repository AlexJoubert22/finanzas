"""Contract tests for the CCXTTrader skeleton.

The skeleton is intentionally non-functional until FASE 9; the only
behaviour that matters today is the **double seatbelt** that prevents
any write from ever reaching a real exchange:

1. ``dry_run=True`` (constructor default)         → blocks.
2. ``trading_enabled=False`` in settings (default) → blocks.

Either being False is enough; both must flip to True for a write to
proceed (and even then the body raises NotImplementedError — that
gets filled in during FASE 9).
"""

from __future__ import annotations

import pytest

from mib.config import get_settings
from mib.sources.ccxt_trader import CCXTTrader


def test_trader_is_unavailable_until_phase_9() -> None:
    t = CCXTTrader()
    assert t.is_available() is False


@pytest.mark.asyncio
async def test_create_order_dry_run_returns_fake_response() -> None:
    t = CCXTTrader(dry_run=True)
    resp = await t.create_order(
        "BTC/USDT",
        side="buy",
        type="limit",
        amount=0.01,
        price=60_000.0,
        client_order_id="mib-test-1",
    )
    assert resp["status"] == "dry-run"
    assert resp["clientOrderId"] == "mib-test-1"
    assert resp["symbol"] == "BTC/USDT"
    assert resp["filled"] == 0.0
    assert resp["info"]["dry_run"] is True


@pytest.mark.asyncio
async def test_second_seatbelt_holds_when_dry_run_false_but_trading_disabled() -> None:
    """Even if a future bug builds a trader with dry_run=False, the
    master kill switch must still block. trading_enabled defaults to
    False — proving the gate stays closed without us touching settings.
    """
    assert get_settings().trading_enabled is False
    t = CCXTTrader(dry_run=False)
    resp = await t.create_order(
        "ETH/USDT", side="sell", type="market", amount=1.0, client_order_id="mib-test-2"
    )
    assert resp["status"] == "dry-run"


@pytest.mark.asyncio
async def test_cancel_order_is_gated() -> None:
    t = CCXTTrader(dry_run=True)
    resp = await t.cancel_order("BTC/USDT", client_order_id="mib-test-3")
    assert resp["status"] == "dry-run"
    assert resp["clientOrderId"] == "mib-test-3"


@pytest.mark.asyncio
async def test_close_position_is_gated() -> None:
    t = CCXTTrader(dry_run=True)
    resp = await t.close_position("BTC/USDT", side="sell", amount=0.5)
    assert resp["status"] == "dry-run"


@pytest.mark.asyncio
async def test_fetch_balance_dry_run_returns_empty_shape() -> None:
    t = CCXTTrader(dry_run=True)
    bal = await t.fetch_balance()
    assert bal["free"] == {}
    assert bal["info"]["dry_run"] is True


@pytest.mark.asyncio
async def test_fetch_positions_dry_run_returns_empty_list() -> None:
    t = CCXTTrader(dry_run=True)
    assert await t.fetch_positions() == []
