"""Contract tests for the CCXTTrader skeleton paths.

FASE 9.1 introduced the triple seatbelt and made ``is_available``
async. FASE 9.2 made ``create_order`` keyword-only with a Decimal
amount/price and required an injected :class:`OrderRepository`.
Sandbox-aware behaviour lives in ``test_ccxt_trader_sandbox.py``;
this file keeps the small surface that is still meaningful for the
no-credentials skeleton path.
"""

from __future__ import annotations

import pytest

from mib.sources.ccxt_trader import CCXTTrader


@pytest.mark.asyncio
async def test_trader_unavailable_without_credentials() -> None:
    """Without API key/secret, ``is_available()`` returns False without
    attempting any network call.
    """
    t = CCXTTrader()
    assert await t.is_available() is False


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
