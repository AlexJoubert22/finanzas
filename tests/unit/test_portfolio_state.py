"""Unit tests for :class:`PortfolioState`.

The cache lives in front of :class:`mib.sources.ccxt_trader.CCXTTrader`.
While ``trading_enabled`` is False (FASE 8 default), the trader's
``fetch_balance`` returns ``{"free":{}, "used":{}, "total":{},
"info":{"dry_run": True}}`` and ``fetch_positions`` returns ``[]``.
The cache must transparently expose this as a snapshot with empty
balances/positions, equity 0 EUR, and ``source="dry-run"``.

These tests use the real CCXTTrader skeleton (no mock) since it
already returns the right empty shape in dry-run.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from mib.models.portfolio import PortfolioSnapshot
from mib.sources.ccxt_trader import CCXTTrader
from mib.trading.portfolio import PortfolioState


def _trader() -> CCXTTrader:
    return CCXTTrader(dry_run=True)


@pytest.mark.asyncio
async def test_snapshot_empty_in_dry_run() -> None:
    state = PortfolioState(_trader())
    snap = await state.snapshot()
    assert isinstance(snap, PortfolioSnapshot)
    assert snap.balances == []
    assert snap.positions == []
    assert snap.equity_quote == Decimal(0)
    assert snap.source == "dry-run"


@pytest.mark.asyncio
async def test_snapshot_is_cached_within_ttl() -> None:
    """Second call within TTL returns the same object (no re-fetch)."""
    state = PortfolioState(_trader(), ttl_seconds=60)
    first = await state.snapshot()
    second = await state.snapshot()
    assert first.last_synced_at == second.last_synced_at


@pytest.mark.asyncio
async def test_snapshot_refreshes_when_ttl_expires() -> None:
    """With ttl_seconds=0, every read forces a fresh fetch."""
    state = PortfolioState(_trader(), ttl_seconds=0)
    first = await state.snapshot()
    # Yield once so the wall clock can move forward.
    await asyncio.sleep(0.01)
    second = await state.snapshot()
    assert second.last_synced_at >= first.last_synced_at


@pytest.mark.asyncio
async def test_explicit_refresh_updates_cache() -> None:
    state = PortfolioState(_trader(), ttl_seconds=60)
    await state.snapshot()
    await asyncio.sleep(0.01)
    refreshed = await state.refresh()
    cached = state.cached
    assert cached is not None
    assert cached.last_synced_at == refreshed.last_synced_at


@pytest.mark.asyncio
async def test_concurrent_reads_dont_double_fetch() -> None:
    """Two coroutines awaiting snapshot() must reuse the same fetch."""

    class CountingTrader(CCXTTrader):
        def __init__(self) -> None:
            super().__init__(dry_run=True)
            self.fetch_count = 0

        async def fetch_balance(self) -> dict[str, Any]:
            self.fetch_count += 1
            return await super().fetch_balance()

    trader = CountingTrader()
    state = PortfolioState(trader, ttl_seconds=60)
    a, b = await asyncio.gather(state.snapshot(), state.snapshot())
    assert a is b  # same cached object
    # The serialised lock means at most 1 fetch per cache miss.
    assert trader.fetch_count == 1


@pytest.mark.asyncio
async def test_balance_with_eur_total_contributes_to_equity() -> None:
    """A non-dry-run balance with EUR rolls up into equity_quote."""

    class FakeTrader(CCXTTrader):
        async def fetch_balance(self) -> dict[str, Any]:
            return {
                "free": {"EUR": 250.0, "BTC": 0.5},
                "used": {"EUR": 0.0, "BTC": 0.0},
                "total": {"EUR": 250.0, "BTC": 0.5},
                "info": {},  # NOT dry_run
            }

        async def fetch_positions(
            self, symbols: list[str] | None = None  # noqa: ARG002
        ) -> list[dict[str, Any]]:
            return []

    state = PortfolioState(FakeTrader(dry_run=True), ttl_seconds=60)
    snap = await state.snapshot()
    assert snap.source == "exchange"
    assert snap.equity_quote == Decimal("250.0")
    assets = {b.asset for b in snap.balances}
    assert assets == {"EUR", "BTC"}


@pytest.mark.asyncio
async def test_zero_balances_dropped_from_snapshot() -> None:
    """Assets with total=0 don't pollute the rendered list."""

    class ZeroDustTrader(CCXTTrader):
        async def fetch_balance(self) -> dict[str, Any]:
            return {
                "free": {"USDT": 100.0, "DOGE": 0.0},
                "used": {"USDT": 0.0, "DOGE": 0.0},
                "total": {"USDT": 100.0, "DOGE": 0.0},
                "info": {},
            }

        async def fetch_positions(
            self, symbols: list[str] | None = None  # noqa: ARG002
        ) -> list[dict[str, Any]]:
            return []

    state = PortfolioState(ZeroDustTrader(dry_run=True), ttl_seconds=60)
    snap = await state.snapshot()
    assets = {b.asset for b in snap.balances}
    assert assets == {"USDT"}


@pytest.mark.asyncio
async def test_position_unrealized_pnl_rolls_into_equity() -> None:
    """Open position contributes its uPnL to equity_quote."""

    class FuturesTrader(CCXTTrader):
        async def fetch_balance(self) -> dict[str, Any]:
            return {
                "free": {"EUR": 1000.0},
                "used": {"EUR": 0.0},
                "total": {"EUR": 1000.0},
                "info": {},
            }

        async def fetch_positions(
            self, symbols: list[str] | None = None  # noqa: ARG002
        ) -> list[dict[str, Any]]:
            return [
                {
                    "symbol": "BTC/USDT:USDT",
                    "side": "long",
                    "contracts": 0.5,
                    "entryPrice": 50_000.0,
                    "markPrice": 51_000.0,
                    "unrealizedPnl": 500.0,
                    "leverage": 2.0,
                }
            ]

    state = PortfolioState(FuturesTrader(dry_run=True), ttl_seconds=60)
    snap = await state.snapshot()
    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "BTC/USDT:USDT"
    assert snap.equity_quote == Decimal("1500.0")  # 1000 EUR + 500 uPnL


@pytest.mark.asyncio
async def test_paper_baseline_floor_applied_when_below() -> None:
    """In PAPER, equity below baseline is floored up."""

    async def resolver():  # type: ignore[no-untyped-def]
        return "paper"  # str works because resolver compares value

    state = PortfolioState(
        _trader(),
        paper_baseline=Decimal("6000"),
        mode_resolver=resolver,
    )
    snap = await state.snapshot()
    # Dry-run trader returns 0 equity; baseline floor lifts it.
    assert snap.equity_quote == Decimal("6000")


@pytest.mark.asyncio
async def test_paper_baseline_not_applied_above() -> None:
    """When equity already above baseline, leave it alone."""

    class FatTrader(CCXTTrader):
        async def fetch_balance(self) -> dict[str, Any]:
            return {
                "free": {"EUR": "8000"},
                "used": {"EUR": "0"},
                "total": {"EUR": "8000"},
                "info": {},
            }

        async def fetch_positions(
            self, symbols: list[str] | None = None  # noqa: ARG002
        ) -> list[dict[str, Any]]:
            return []

    async def resolver():  # type: ignore[no-untyped-def]
        return "paper"

    state = PortfolioState(
        FatTrader(dry_run=True),
        paper_baseline=Decimal("6000"),
        mode_resolver=resolver,
    )
    snap = await state.snapshot()
    assert snap.equity_quote == Decimal("8000")


@pytest.mark.asyncio
async def test_paper_baseline_not_applied_outside_paper() -> None:
    async def resolver():  # type: ignore[no-untyped-def]
        return "shadow"

    state = PortfolioState(
        _trader(),
        paper_baseline=Decimal("6000"),
        mode_resolver=resolver,
    )
    snap = await state.snapshot()
    assert snap.equity_quote == Decimal(0)  # no floor outside PAPER


@pytest.mark.asyncio
async def test_snapshot_is_immutable() -> None:
    """Frozen Pydantic raises ``ValidationError`` on attribute set."""
    from pydantic import ValidationError  # noqa: PLC0415

    state = PortfolioState(_trader())
    snap = await state.snapshot()
    with pytest.raises(ValidationError):
        snap.equity_quote = Decimal(999)  # type: ignore[misc]
