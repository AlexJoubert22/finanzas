"""In-memory portfolio state cache, refreshed from the exchange.

The scheduler runs :func:`portfolio_sync_job` every 30 seconds and
calls :meth:`PortfolioState.refresh`. Reads (RiskManager gates,
``/portfolio`` endpoint, ``/status`` Telegram) hit the cache through
:meth:`snapshot`, which auto-refreshes if the cached value is older
than ``ttl_seconds``.

Concurrency: a single ``asyncio.Lock`` guards both refresh and
read-with-auto-refresh paths so two simultaneous callers cannot
trigger duplicate exchange fetches. Lock is fine-grained — held only
during the refresh, not for the lifetime of the snapshot read.

Until FASE 9 wires real CCXT credentials, ``CCXTTrader`` returns the
dry-run empty shape (``{free:{}, used:{}, total:{}}`` and ``[]``).
The cache reflects that as ``source="dry-run"`` so any consumer can
detect "we are not actually connected" without inspecting flags.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from mib.logger import logger
from mib.models.portfolio import (
    Balance,
    PortfolioSnapshot,
    Position,
    SnapshotSource,
)
from mib.sources.ccxt_trader import CCXTTrader

DEFAULT_TTL_SECONDS: int = 30


class PortfolioState:
    """Cached :class:`PortfolioSnapshot` with TTL-bounded auto-refresh."""

    def __init__(
        self,
        trader: CCXTTrader,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        quote_currency: str = "EUR",
        paper_baseline: Decimal | None = None,
        mode_resolver: Any = None,
    ) -> None:
        self._trader = trader
        self._ttl = timedelta(seconds=ttl_seconds)
        self._quote = quote_currency
        self._cache: PortfolioSnapshot | None = None
        self._lock = asyncio.Lock()
        # PAPER prep: when mode_resolver returns PAPER and computed
        # equity is below paper_baseline, the snapshot's equity_quote
        # is padded up to the baseline. Sizing and PnL/% remain
        # anchored to a stable reference even after testnet resets.
        self._paper_baseline = paper_baseline
        self._mode_resolver = mode_resolver

    async def snapshot(self) -> PortfolioSnapshot:
        """Return current snapshot, refreshing if stale or absent."""
        async with self._lock:
            if self._is_fresh():
                # Tell mypy we just verified non-None inside _is_fresh.
                assert self._cache is not None
                return self._cache
            self._cache = await self._fetch_snapshot()
            return self._cache

    async def refresh(self) -> PortfolioSnapshot:
        """Force a refresh; used by the scheduler job each tick."""
        async with self._lock:
            self._cache = await self._fetch_snapshot()
            return self._cache

    @property
    def cached(self) -> PortfolioSnapshot | None:
        """Last cached snapshot without triggering a refresh.

        Reserved for diagnostics (``/portfolio`` falls back to this
        when exchange is unreachable in degraded modes — FASE 9+).
        """
        return self._cache

    # ─── Internal ──────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        if self._cache is None:
            return False
        return (datetime.now(UTC) - self._cache.last_synced_at) < self._ttl

    async def _fetch_snapshot(self) -> PortfolioSnapshot:
        raw_balance: dict[str, Any] = await self._trader.fetch_balance()
        raw_positions: list[dict[str, Any]] = await self._trader.fetch_positions()

        source: SnapshotSource = (
            "dry-run"
            if raw_balance.get("info", {}).get("dry_run") is True
            else "exchange"
        )
        balances = _parse_balances(raw_balance)
        positions = _parse_positions(raw_positions)
        equity = _compute_equity_quote(balances, positions, quote=self._quote)
        equity = await self._maybe_floor_to_paper_baseline(equity)

        return PortfolioSnapshot(
            balances=balances,
            positions=positions,
            equity_quote=equity,
            last_synced_at=datetime.now(UTC),
            source=source,
        )

    async def _maybe_floor_to_paper_baseline(
        self, equity: Decimal
    ) -> Decimal:
        """When in PAPER and equity < paper_baseline, return the
        baseline. No-op outside PAPER or when baseline is unset.
        """
        if self._paper_baseline is None or self._mode_resolver is None:
            return equity
        try:
            current_mode = await self._mode_resolver()
        except Exception as exc:  # noqa: BLE001
            logger.debug("portfolio: mode_resolver failed: {}", exc)
            return equity
        # Avoid importing TradingMode here to keep cycles out; compare
        # against the .value the resolver returns.
        mode_value = getattr(current_mode, "value", current_mode)
        if mode_value != "paper":
            return equity
        if equity < self._paper_baseline:
            logger.debug(
                "portfolio: PAPER equity {} < baseline {} — flooring",
                equity, self._paper_baseline,
            )
            return self._paper_baseline
        return equity


# ─── Parsers (CCXT shape → typed models) ───────────────────────────

def _parse_balances(raw: dict[str, Any]) -> list[Balance]:
    """Translate a CCXT balance payload to a list of :class:`Balance`.

    CCXT shape: ``{"free": {asset: amount, ...}, "used": {...},
    "total": {...}}``. Assets present in ``total`` with non-zero value
    become rows; zero-only assets are dropped to keep the snapshot
    compact.
    """
    totals = raw.get("total") or {}
    free = raw.get("free") or {}
    used = raw.get("used") or {}
    rows: list[Balance] = []
    for asset, total in totals.items():
        total_dec = _to_decimal(total)
        if total_dec == 0:
            continue
        rows.append(
            Balance(
                asset=str(asset),
                free=_to_decimal(free.get(asset, 0)),
                used=_to_decimal(used.get(asset, 0)),
                total=total_dec,
            )
        )
    return rows


def _parse_positions(raw: list[dict[str, Any]]) -> list[Position]:
    """Translate CCXT positions to typed :class:`Position` rows."""
    out: list[Position] = []
    for p in raw:
        amount = _to_decimal(p.get("contracts") or p.get("amount") or 0)
        if amount == 0:
            continue
        side_raw = (p.get("side") or "").lower()
        side: str = "long" if side_raw == "long" else "short"
        out.append(
            Position(
                symbol=str(p.get("symbol") or ""),
                side=side,  # type: ignore[arg-type]
                amount=amount,
                entry_price=_to_decimal(p.get("entryPrice") or 0),
                mark_price=_to_decimal(p.get("markPrice") or 0),
                unrealized_pnl=_to_decimal(p.get("unrealizedPnl") or 0),
                leverage=float(p.get("leverage") or 1.0),
            )
        )
    return out


def _compute_equity_quote(
    balances: list[Balance],
    positions: list[Position],
    *,
    quote: str,
) -> Decimal:
    """Best-effort equity in ``quote`` currency.

    FASE 8.2 deliberately keeps this simple: if a balance row matches
    the configured quote currency, use its total; sum unrealized PnL
    of positions. Any non-quote balance is left as 0 here — a proper
    multi-asset valuation needs live spot prices and lands in FASE 9
    when real exchange data is flowing. In dry-run mode the function
    returns 0 cleanly because there are no balances to sum.
    """
    equity = Decimal(0)
    for b in balances:
        if b.asset.upper() == quote.upper():
            equity += b.total
    for p in positions:
        equity += p.unrealized_pnl
    return equity


def _to_decimal(value: Any) -> Decimal:
    """Coerce ints/floats/strings to ``Decimal`` without surprise.

    CCXT may return ``None`` for absent fields; this returns ``0`` so
    callers don't have to branch.
    """
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio: cannot coerce {!r} to Decimal: {}", value, exc)
        return Decimal(0)
