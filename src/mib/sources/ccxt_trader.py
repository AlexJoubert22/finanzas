"""CCXT-backed order executor — write side of the trading split.

This is a **deliberate skeleton**. The methods are stubbed for FASE 9;
today they only honour the dry-run gate so phases 7 and 8 can wire
calls through the trader without any risk of touching a real
exchange.

Design constraints baked in from day one:

- **Separate keys.** ``CCXTReader`` runs without API credentials.
  ``CCXTTrader`` requires an order-permitted key; loading it from the
  same env var as the reader would be a misconfiguration. The trader
  reads its own ``ccxt_trader_*`` settings (added in FASE 9).
- **IP whitelisting.** When configuring the API key on Binance/Bybit,
  pin it to the BambuServer's IP. This module does not try to enforce
  that — it is a deployment-time control.
- **Sandbox flag.** When ``trading_mode == PAPER`` the underlying
  exchange client must call ``set_sandbox_mode(True)``. The current
  skeleton accepts the flag in the constructor but does not yet wire
  the exchange object (FASE 9).
- **Double seatbelt.** Every write method checks BOTH ``self._dry_run``
  AND ``get_settings().trading_enabled``. Either one being False
  short-circuits to a fake response — even an explicitly-built trader
  with ``dry_run=False`` cannot send orders unless the master kill
  switch is also flipped on.
- **Idempotency.** ``client_order_id`` is the contract: callers
  generate a deterministic UUID per signal; on retry the exchange
  returns the same order rather than duplicating.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

from mib.config import get_settings
from mib.logger import logger


class CCXTTrader:
    """Order executor stub. Methods raise NotImplementedError until FASE 9."""

    name: ClassVar[str] = "ccxt_trader"

    def __init__(
        self,
        exchange_id: str = "binance",
        *,
        api_key: str = "",
        api_secret: str = "",
        dry_run: bool = True,
        sandbox: bool = True,
    ) -> None:
        self._exchange_id = exchange_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._dry_run = dry_run
        self._sandbox = sandbox
        self._exchange: Any = None  # bound in FASE 9

    def is_available(self) -> bool:
        """Stub: until FASE 9 the trader is never available."""
        return False

    # ─── Internal: dry-run gate ─────────────────────────────────────────

    def _gate_blocks_writes(self) -> bool:
        """True when this call must NOT reach the exchange.

        Two seatbelts: the per-instance ``dry_run`` and the global
        ``trading_enabled`` setting. Either one being False blocks the
        write. This is intentional — accidentally building a trader
        with ``dry_run=False`` still cannot trade unless the operator
        also flipped the master switch.
        """
        return self._dry_run or not get_settings().trading_enabled

    @staticmethod
    def _fake_order_response(payload: dict[str, Any]) -> dict[str, Any]:
        """Return a CCXT-shaped dict that mimics a successful order.

        The shape mirrors what ``ccxt.async_support.Exchange.create_order``
        returns (id, clientOrderId, symbol, type, side, amount, price,
        status, timestamp, datetime). Callers in FASE 8/9 can consume
        this without branching on dry-run vs. live.
        """
        now = datetime.now(UTC)
        return {
            "id": f"dry-run-{uuid4().hex[:12]}",
            "clientOrderId": payload.get("clientOrderId"),
            "symbol": payload.get("symbol"),
            "type": payload.get("type"),
            "side": payload.get("side"),
            "amount": payload.get("amount"),
            "price": payload.get("price"),
            "filled": 0.0,
            "remaining": payload.get("amount"),
            "status": "dry-run",
            "timestamp": int(now.timestamp() * 1000),
            "datetime": now.isoformat(),
            "info": {"dry_run": True, "payload": payload},
        }

    # ─── Order management API ───────────────────────────────────────────

    async def create_order(
        self,
        symbol: str,
        side: str,
        type: str,  # noqa: A002 — matches ccxt parameter name
        amount: float,
        *,
        price: float | None = None,
        client_order_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place an order. Falls through to a fake response when gated."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "amount": amount,
            "price": price,
            "clientOrderId": client_order_id,
            "params": params or {},
        }
        if self._gate_blocks_writes():
            logger.info("ccxt-trader: dry_run, would execute: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError("CCXTTrader.create_order — wired in FASE 9")

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an open order by exchange id or client order id."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "id": exchange_order_id,
            "clientOrderId": client_order_id,
            "op": "cancel",
        }
        if self._gate_blocks_writes():
            logger.info("ccxt-trader: dry_run, would execute: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError("CCXTTrader.cancel_order — wired in FASE 9")

    async def close_position(
        self,
        symbol: str,
        side: str,
        amount: float,
    ) -> dict[str, Any]:
        """Close (or reduce) an open position at market."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "op": "close_position",
        }
        if self._gate_blocks_writes():
            logger.info("ccxt-trader: dry_run, would execute: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError("CCXTTrader.close_position — wired in FASE 9")

    # ─── Account state API (auth-required reads) ───────────────────────

    async def fetch_balance(self) -> dict[str, Any]:
        """Snapshot of free/used/total balances per asset."""
        if self._gate_blocks_writes():
            logger.debug("ccxt-trader: dry_run, returning empty balance")
            return {"free": {}, "used": {}, "total": {}, "info": {"dry_run": True}}
        raise NotImplementedError("CCXTTrader.fetch_balance — wired in FASE 9")

    async def fetch_positions(
        self, symbols: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Open positions (futures/margin only). Spot exchanges return empty."""
        if self._gate_blocks_writes():
            logger.debug(
                "ccxt-trader: dry_run, returning empty positions (symbols={})", symbols
            )
            return []
        raise NotImplementedError("CCXTTrader.fetch_positions — wired in FASE 9")

    async def fetch_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a known order against the exchange's view."""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "id": exchange_order_id,
            "clientOrderId": client_order_id,
            "op": "fetch_order",
        }
        if self._gate_blocks_writes():
            logger.debug("ccxt-trader: dry_run, would fetch: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError("CCXTTrader.fetch_order — wired in FASE 9")

    async def close(self) -> None:
        """Release the underlying aiohttp session, if any."""
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.warning("ccxt-trader close failed: {}", exc)
            self._exchange = None
