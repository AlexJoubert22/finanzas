"""CCXT-backed order executor — write side of the trading split.

FASE 9.1 wires the real connection to Binance Testnet (sandbox).
Reads (``fetch_balance``, ``fetch_positions``, ``fetch_order``) hit
the live testnet endpoint when credentials are present and the
instance is not in dry-run mode. Writes (``create_order``,
``cancel_order``, ``close_position``) are gated by a **triple
seatbelt**.

Triple seatbelt (every write checks all three):

1. ``trading_enabled`` (global ``Settings``) — operator's master
   kill switch. Default ``False``; flipped only post-FASE-14.
2. ``self._dry_run`` (per-instance) — defensive flag that callers
   can flip on without touching settings. Default tracks
   ``not trading_enabled``.
3. ``self._is_sandbox`` — auto-detected from ``base_url`` containing
   ``"testnet"`` or ``"sandbox"`` (case-insensitive). Production
   exchange URLs CANNOT pass this gate; relaxing it requires an
   explicit FASE 14 patch with operator review.

Reads are gated only by ``dry_run`` (preserves FASE 7/8 test
behaviour where ``dry_run=True`` returns the empty CCXT shape so
``PortfolioState`` doesn't hit the network).

Idempotency for orders: callers pass ``client_order_id`` (deterministic
per signal, computed in FASE 9.2). The exchange returns the same
order on retry rather than duplicating.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from mib.config import get_settings
from mib.logger import logger
from mib.trading.orders import (
    OrderInputs,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)

if TYPE_CHECKING:  # pragma: no cover
    from mib.trading.order_repo import OrderRepository

#: Bound for ``is_available()`` — the operator wants a quick yes/no
#: at boot, not a 30-second hang if testnet is sluggish.
_PING_TIMEOUT_SECONDS: float = 2.0


class CCXTTrader:
    """Order executor with triple-seatbelt gating."""

    name: ClassVar[str] = "ccxt_trader"

    def __init__(
        self,
        exchange_id: str = "binance",
        *,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "",
        dry_run: bool = True,
        order_repo: OrderRepository | None = None,
    ) -> None:
        self._exchange_id = exchange_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url
        self._dry_run = dry_run
        self._is_sandbox = _detect_sandbox(base_url)
        self._exchange: Any = None  # lazy
        # Optional in tests that exercise the seatbelt without persisting.
        self._order_repo: OrderRepository | None = order_repo

    @property
    def _exchange_label(self) -> str:
        """Stable string used as ``orders.exchange_id`` column value.

        Distinguishes ``binance`` (production) from ``binance_sandbox``
        so reconciliation queries can filter cleanly.
        """
        return f"{self._exchange_id}_sandbox" if self._is_sandbox else self._exchange_id

    @property
    def is_sandbox(self) -> bool:
        """True when the configured ``base_url`` targets a testnet/sandbox."""
        return self._is_sandbox

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    # ─── Health probe ──────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Bounded ping. True iff credentials are set AND the exchange
        responds within ``_PING_TIMEOUT_SECONDS``.

        Never raises — returns ``False`` on any error so the caller
        (e.g. ``/preflight``) can render a degraded state cleanly.
        """
        if not self.has_credentials:
            return False
        try:
            exchange = await self._ensure_exchange()
            # ``fetch_status`` is the cheapest authenticated probe.
            await asyncio.wait_for(
                exchange.fetch_status(), timeout=_PING_TIMEOUT_SECONDS
            )
            return True
        except Exception as exc:  # noqa: BLE001 — diagnostic, never raise
            logger.info("ccxt-trader: is_available probe failed: {}", exc)
            return False

    # ─── Triple seatbelt ───────────────────────────────────────────

    def _gate_blocks_writes(self) -> bool:
        """All three seatbelts must be open for a write to proceed.

        Order is intentional: dry_run is the cheapest check (no DB),
        trading_enabled is a settings read, is_sandbox is an instance
        attribute. We log the FIRST blocker rather than every blocker
        so the log line names the closest defence that fired.
        """
        if self._dry_run:
            return True
        if not get_settings().trading_enabled:
            return True
        # Third seatbelt: hard block on non-sandbox URLs until FASE 14
        # explicitly relaxes it.
        return not self._is_sandbox

    def _gate_blocks_reads(self) -> bool:
        """Reads are gated only by ``dry_run`` (preserves FASE 7/8
        offline-test behaviour). Tests can still construct a trader
        with ``dry_run=True`` and get the empty CCXT shape.
        """
        return self._dry_run

    @staticmethod
    def _fake_order_response(payload: dict[str, Any]) -> dict[str, Any]:
        """CCXT-shaped fake. Used when the seatbelt blocks a write."""
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

    # ─── Lazy exchange bootstrap ───────────────────────────────────

    async def _ensure_exchange(self) -> Any:
        if self._exchange is not None:
            return self._exchange
        if not self.has_credentials:
            raise RuntimeError(
                "CCXTTrader: no credentials configured; cannot connect"
            )
        # Lazy import: ``ccxt.async_support`` is heavy.
        import importlib  # noqa: PLC0415

        try:
            mod = importlib.import_module(
                f"ccxt.async_support.{self._exchange_id}"
            )
            exchange_cls = getattr(mod, self._exchange_id)
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                f"Unknown CCXT exchange id: {self._exchange_id}"
            ) from exc

        config: dict[str, Any] = {
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "options": {"defaultType": "spot"},
            "enableRateLimit": True,
            "timeout": 30_000,
        }
        exchange = exchange_cls(config)
        if self._is_sandbox:
            exchange.set_sandbox_mode(True)
        self._exchange = exchange
        logger.info(
            "ccxt-trader: connected exchange={} sandbox={} base_url={}",
            self._exchange_id,
            self._is_sandbox,
            self._base_url or "(default)",
        )
        return self._exchange

    # ─── Order management API ──────────────────────────────────────

    async def create_order(
        self,
        *,
        signal_id: int,
        symbol: str,
        side: OrderSide,
        type: OrderType,  # noqa: A002 — matches ccxt parameter name
        amount: Decimal,
        price: Decimal | None = None,
        reduce_only: bool = False,
        extra_params: dict[str, Any] | None = None,
    ) -> OrderResult:
        """Place an order with idempotent client_order_id + audit trail.

        Flow:

        1. Persist an :class:`OrderRow` (status='created', 'created'
           event written) with a deterministic ``client_order_id``
           derived from (signal_id, symbol, side, type, amount, price,
           reduce_only). Retries on the same params hit the UNIQUE
           constraint and return the existing row.
        2. Triple seatbelt check. If blocked, log + return
           :class:`OrderResult` with ``status='created'`` and a clear
           ``reason``. The exchange is never touched.
        3. Otherwise call ``exchange.create_order(...)`` with the
           Binance ``newClientOrderId`` param.
        4. On success: ``transition('submitted')`` with
           ``exchange_order_id`` populated.
        5. On failure: ``transition('rejected')`` (4xx-shaped) or
           ``transition('failed')`` (timeouts/network) with the
           exception message captured in the audit row.
        """
        if self._order_repo is None:
            raise RuntimeError(
                "CCXTTrader.create_order requires an OrderRepository — "
                "construct via api.dependencies.get_ccxt_trader()."
            )

        inputs = OrderInputs(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            type=type,
            amount=amount,
            price=price,
            reduce_only=reduce_only,
            extra_params=extra_params or {},
        )
        raw_payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "amount": str(amount),
            "price": str(price) if price is not None else None,
            "reduce_only": reduce_only,
            "extra_params": extra_params or {},
        }

        existing = await self._order_repo.add_or_get(
            inputs,
            exchange_id=self._exchange_label,
            raw_payload=raw_payload,
        )
        # Idempotent return: if the existing row is past 'created',
        # the previous attempt already submitted. Don't re-submit.
        if existing.status != "created":
            logger.info(
                "ccxt-trader: idempotent return order_id={} status={}",
                existing.order_id,
                existing.status,
            )
            return existing

        if self._gate_blocks_writes():
            logger.info(
                "ccxt-trader: triple seatbelt blocked write for order_id={}",
                existing.order_id,
            )
            updated = await self._order_repo.transition(
                existing.order_id,
                "cancelled",
                actor="ccxt-trader:gate",
                event_type="cancelled",
                reason="triple-seatbelt blocked write (dry-run / trading_enabled / sandbox)",
                expected_from_status="created",
            )
            assert updated is not None
            return _result_with_reason(
                updated, "blocked by triple seatbelt"
            )

        # Real path: send to exchange.
        exchange = await self._ensure_exchange()
        ccxt_params: dict[str, Any] = dict(extra_params or {})
        ccxt_params["newClientOrderId"] = existing.client_order_id
        if reduce_only:
            ccxt_params["reduceOnly"] = True

        try:
            response: dict[str, Any] = await exchange.create_order(
                symbol,
                type,
                side,
                float(amount),
                float(price) if price is not None else None,
                ccxt_params,
            )
        except Exception as exc:  # noqa: BLE001
            # ``exc.__class__`` rather than ``type(exc)`` because the
            # local parameter ``type`` shadows the builtin and would
            # confuse static analysis.
            error_class = exc.__class__.__name__
            logger.warning(
                "ccxt-trader: exchange create_order failed order_id={} {}: {}",
                existing.order_id,
                error_class,
                exc,
            )
            terminal_status = _classify_failure(error_class)
            updated = await self._order_repo.transition(
                existing.order_id,
                terminal_status,
                actor="ccxt-trader:exchange",
                event_type=terminal_status,
                reason=f"{error_class}: {exc}",
                expected_from_status="created",
                raw_response={"error": error_class, "message": str(exc)},
            )
            assert updated is not None
            return _result_with_reason(updated, f"{error_class}: {exc}")

        exchange_order_id = str(response.get("id") or "") or None
        updated = await self._order_repo.transition(
            existing.order_id,
            "submitted",
            actor="ccxt-trader:exchange",
            event_type="submitted",
            reason=None,
            expected_from_status="created",
            exchange_order_id=exchange_order_id,
            raw_response=response,
        )
        assert updated is not None
        logger.info(
            "ccxt-trader: submitted order_id={} exchange_id={} client_id={}",
            updated.order_id,
            updated.exchange_order_id,
            updated.client_order_id,
        )
        return updated

    async def cancel_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "id": exchange_order_id,
            "clientOrderId": client_order_id,
            "op": "cancel",
        }
        if self._gate_blocks_writes():
            logger.info("ccxt-trader: gated, would execute: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError(
            "CCXTTrader.cancel_order — real path wired in FASE 9.2"
        )

    async def close_position(
        self,
        symbol: str,
        side: str,
        amount: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "op": "close_position",
        }
        if self._gate_blocks_writes():
            logger.info("ccxt-trader: gated, would execute: {}", payload)
            return self._fake_order_response(payload)
        raise NotImplementedError(
            "CCXTTrader.close_position — real path wired in FASE 9.2"
        )

    # ─── Account state API (auth-required reads) ───────────────────

    async def fetch_balance(self) -> dict[str, Any]:
        if self._gate_blocks_reads():
            logger.debug("ccxt-trader: dry_run, returning empty balance")
            return {"free": {}, "used": {}, "total": {}, "info": {"dry_run": True}}
        exchange = await self._ensure_exchange()
        result: dict[str, Any] = await exchange.fetch_balance()
        return result

    async def fetch_positions(
        self, symbols: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if self._gate_blocks_reads():
            logger.debug(
                "ccxt-trader: dry_run, returning empty positions (symbols={})",
                symbols,
            )
            return []
        exchange = await self._ensure_exchange()
        # Spot exchanges return [] from fetch_positions; futures return data.
        try:
            positions = await exchange.fetch_positions(symbols)
            # ccxt may return None for spot; coerce to empty.
            return list(positions or [])
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "ccxt-trader: fetch_positions not supported on {}: {}",
                self._exchange_id,
                exc,
            )
            return []

    async def fetch_order(
        self,
        symbol: str,
        *,
        exchange_order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "id": exchange_order_id,
            "clientOrderId": client_order_id,
            "op": "fetch_order",
        }
        if self._gate_blocks_reads():
            logger.debug("ccxt-trader: dry_run, would fetch: {}", payload)
            return self._fake_order_response(payload)
        exchange = await self._ensure_exchange()
        params: dict[str, Any] = {}
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        result: dict[str, Any] = await exchange.fetch_order(
            exchange_order_id, symbol, params=params
        )
        return result

    async def close(self) -> None:
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ccxt-trader close failed: {}", exc)
            self._exchange = None


def _result_with_reason(result: OrderResult, reason: str) -> OrderResult:
    """Return ``result`` with ``reason`` populated (frozen dataclass)."""
    from dataclasses import replace  # noqa: PLC0415

    return replace(result, reason=reason)


def _classify_failure(exception_class_name: str) -> OrderStatus:
    """Bucket exchange errors into ``rejected`` (4xx) vs ``failed``
    (network/timeout). Used by the audit trail.
    """
    name = exception_class_name.lower()
    if "timeout" in name or "network" in name or "connection" in name:
        return "failed"
    return "rejected"


def _detect_sandbox(base_url: str) -> bool:
    """True iff ``base_url`` clearly targets a testnet/sandbox.

    Empty base_url → True (default for the FASE 8 skeleton path
    where the trader is never asked to hit a real exchange). A real
    Binance/Bybit production URL like ``api.binance.com`` returns
    False, which keeps the third seatbelt closed.
    """
    if not base_url:
        return True
    needle = base_url.lower()
    return ("testnet" in needle) or ("sandbox" in needle)
