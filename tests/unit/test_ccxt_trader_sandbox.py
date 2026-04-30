"""FASE 9.1 — Triple seatbelt + sandbox detection contract tests.

Three independent gates control writes:

1. ``dry_run`` per-instance flag.
2. ``trading_enabled`` global setting.
3. ``is_sandbox`` (auto-detected from ``base_url``).

Only when ALL THREE are open does a write proceed. Reads are gated
only by ``dry_run`` (preserves FASE 7/8 offline-test behaviour).

These tests cover the full 2³ = 8 truth table for writes and the
expected open/closed combinations for each cell.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mib.config import get_settings
from mib.sources.ccxt_trader import CCXTTrader

# ─── Sandbox detection ───────────────────────────────────────────

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://testnet.binance.vision", True),
        ("https://api.testnet.bybit.com", True),
        ("https://sandbox.example.com", True),
        ("https://api-sandbox.kraken.com", True),
        ("https://api.binance.com", False),
        ("https://api.bybit.com", False),
        ("", True),  # empty base_url → default-safe (skeleton path)
        ("https://TESTNET.binance.vision", True),  # case-insensitive
    ],
)
def test_sandbox_detection_from_base_url(url: str, expected: bool) -> None:
    t = CCXTTrader(base_url=url, api_key="k", api_secret="s")
    assert t.is_sandbox is expected


def test_sandbox_default_when_no_url_given() -> None:
    """Constructor default keeps the third seatbelt closed by erring
    on the side of "this is sandbox-shaped" — combined with empty
    credentials, the trader is unavailable anyway, so this matches
    the FASE 7/8 skeleton behaviour.
    """
    t = CCXTTrader()
    assert t.is_sandbox is True
    assert t.has_credentials is False


# ─── Triple seatbelt: full 2^3 truth table ────────────────────────

def _make_trader(*, dry_run: bool, is_sandbox: bool) -> CCXTTrader:
    base_url = "https://testnet.binance.vision" if is_sandbox else "https://api.binance.com"
    return CCXTTrader(
        api_key="k",
        api_secret="s",
        base_url=base_url,
        dry_run=dry_run,
    )


_TRUTH_TABLE = [
    # (dry_run, trading_enabled, is_sandbox, expected_blocked)
    (True, True, True, True),    # dry_run blocks
    (True, True, False, True),   # dry_run + sandbox would both block
    (True, False, True, True),   # dry_run + trading would both block
    (True, False, False, True),  # all three would block
    (False, True, True, False),  # ✓ ONLY case that proceeds
    (False, True, False, True),  # sandbox seatbelt holds
    (False, False, True, True),  # trading_enabled seatbelt holds
    (False, False, False, True), # trading + sandbox both hold
]


@pytest.mark.parametrize(
    "dry_run, trading_enabled, is_sandbox, expected_blocked",
    _TRUTH_TABLE,
)
def test_triple_seatbelt_truth_table(
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    trading_enabled: bool,
    is_sandbox: bool,
    expected_blocked: bool,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(
        settings, "trading_enabled", trading_enabled, raising=False
    )
    t = _make_trader(dry_run=dry_run, is_sandbox=is_sandbox)
    assert t._gate_blocks_writes() is expected_blocked  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_order_without_repo_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw CCXTTrader (no OrderRepository) must refuse create_order
    with a guidance error pointing at ``get_ccxt_trader()`` — the
    repo is required by FASE 9.2 so every call lands an audit row.
    """
    from decimal import Decimal  # noqa: PLC0415

    settings = get_settings()
    monkeypatch.setattr(settings, "trading_enabled", True, raising=False)
    t = _make_trader(dry_run=False, is_sandbox=True)  # no repo
    with pytest.raises(RuntimeError, match="OrderRepository"):
        await t.create_order(
            signal_id=1,
            symbol="BTC/USDT",
            side="buy",
            type="limit",
            amount=Decimal("0.001"),
            price=Decimal(60_000),
        )


# ─── is_available — ping behaviour ────────────────────────────────

@pytest.mark.asyncio
async def test_is_available_false_without_credentials() -> None:
    t = CCXTTrader()
    assert await t.is_available() is False


@pytest.mark.asyncio
async def test_is_available_true_when_ping_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With keys and a fake exchange whose fetch_status returns
    quickly, is_available is True. Mocks the lazy ``_ensure_exchange``
    so no real network is hit.
    """

    class _FakeExchange:
        async def fetch_status(self) -> dict[str, Any]:
            return {"status": "ok"}

    t = CCXTTrader(
        api_key="k", api_secret="s",
        base_url="https://testnet.binance.vision",
    )
    monkeypatch.setattr(
        t, "_ensure_exchange", AsyncMock(return_value=_FakeExchange())
    )
    assert await t.is_available() is True


@pytest.mark.asyncio
async def test_is_available_false_on_ping_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio  # noqa: PLC0415

    class _SlowExchange:
        async def fetch_status(self) -> dict[str, Any]:
            await asyncio.sleep(5)
            return {}

    t = CCXTTrader(
        api_key="k", api_secret="s",
        base_url="https://testnet.binance.vision",
    )
    monkeypatch.setattr(
        t, "_ensure_exchange", AsyncMock(return_value=_SlowExchange())
    )
    assert await t.is_available() is False


@pytest.mark.asyncio
async def test_is_available_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any exception during the probe → False, never propagates."""
    monkeypatch.setattr(
        CCXTTrader,
        "_ensure_exchange",
        AsyncMock(side_effect=RuntimeError("DNS down")),
    )
    t = CCXTTrader(
        api_key="k", api_secret="s",
        base_url="https://testnet.binance.vision",
    )
    assert await t.is_available() is False


# ─── Reads bypass triple seatbelt (only dry_run gates them) ──────

@pytest.mark.asyncio
async def test_fetch_balance_returns_empty_in_dry_run() -> None:
    """The FASE 8.2 contract: ``dry_run=True`` returns the empty CCXT
    shape so ``PortfolioState`` never hits the network in tests.
    """
    t = CCXTTrader(dry_run=True, api_key="k", api_secret="s")
    balance = await t.fetch_balance()
    assert balance == {
        "free": {}, "used": {}, "total": {}, "info": {"dry_run": True}
    }


@pytest.mark.asyncio
async def test_fetch_positions_returns_empty_in_dry_run() -> None:
    t = CCXTTrader(dry_run=True, api_key="k", api_secret="s")
    assert await t.fetch_positions() == []
