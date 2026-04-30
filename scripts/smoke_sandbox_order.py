"""End-to-end smoke test against Binance Testnet (FASE 9 close).

Walks through the full FASE 9.1-9.6 flow against the live sandbox:

    1. Place a tiny limit BUY at a price likely to fill (mid).
    2. Wait for fill via direct ``fetch_order`` polls.
    3. Place the protective stop_market with reduceOnly.
    4. Cancel the stop and the entry if anything is still resting.

Defaults: BTC/USDT, 0.001 BTC, mid price computed from the order book.
The script aborts before any write if the resolved base_url does not
contain ``testnet`` / ``sandbox``.

Sync ccxt is used to dodge the aiohttp DNS quirk in some constrained
shells (same trick as ``smoke_sandbox_ping.py``).

Run::

    PYTHONUTF8=1 uv run python scripts/smoke_sandbox_order.py
"""

from __future__ import annotations

import sys
import time
import uuid

from dotenv import dotenv_values


def _color(s: str, ok: bool = True) -> str:
    code = "\033[32m" if ok else "\033[31m"
    return f"{code}{s}\033[0m"


def main() -> int:  # noqa: C901 — straight-line script intentionally
    env = dotenv_values(".env")
    api_key = (env.get("BINANCE_SANDBOX_API_KEY") or "").strip()
    secret = (env.get("BINANCE_SANDBOX_SECRET") or "").strip()
    base_url = env.get("BINANCE_SANDBOX_BASE_URL") or "https://testnet.binance.vision"

    if not api_key or not secret:
        print(_color("FAIL: BINANCE_SANDBOX_API_KEY/SECRET missing in .env", ok=False))
        return 2
    if "testnet" not in base_url.lower() and "sandbox" not in base_url.lower():
        print(
            _color(
                f"FAIL: base_url {base_url!r} not testnet/sandbox — refusing to place orders.",
                ok=False,
            )
        )
        return 3

    try:
        import ccxt  # noqa: PLC0415
    except ImportError as exc:
        print(_color(f"FAIL: ccxt not installed ({exc})", ok=False))
        return 4

    exchange = ccxt.binance({
        "apiKey": api_key,
        "secret": secret,
        "options": {"defaultType": "spot"},
        "enableRateLimit": True,
        "timeout": 30_000,
    })
    exchange.set_sandbox_mode(True)

    symbol = "BTC/USDT"
    amount = 0.001
    print(f"Connecting to {base_url} ...")

    # Snapshot orderbook to choose a fillable limit price.
    try:
        ticker = exchange.fetch_ticker(symbol)
    except Exception as exc:  # noqa: BLE001
        print(_color(f"FAIL: fetch_ticker {symbol}: {exc}", ok=False))
        return 5
    last = float(ticker.get("last") or 0.0)
    bid = float(ticker.get("bid") or last)
    if last <= 0 or bid <= 0:
        print(_color(f"FAIL: invalid ticker payload {ticker}", ok=False))
        return 6
    # Aggressive limit close to last → likely fills on testnet.
    entry_price = round(last * 1.001, 2)
    stop_price = round(last * 0.95, 2)
    print(f"  last={last:.2f}  entry_limit={entry_price:.2f}  stop_trigger={stop_price:.2f}")

    client_id_entry = f"mib-smoke-{uuid.uuid4().hex[:8]}"
    print(f"\n[1] Placing limit BUY {amount} {symbol} @ {entry_price} (clientOrderId={client_id_entry}) ...")
    try:
        entry = exchange.create_order(
            symbol,
            "limit",
            "buy",
            amount,
            entry_price,
            {"newClientOrderId": client_id_entry},
        )
    except Exception as exc:  # noqa: BLE001
        print(_color(f"FAIL: create_order entry: {exc}", ok=False))
        return 7
    entry_id = str(entry.get("id") or "")
    print(_color(f"OK  entry submitted: id={entry_id} status={entry.get('status')}"))

    # Poll for fill.
    print("\n[2] Polling fetch_order for fill (max 30s) ...")
    deadline = time.monotonic() + 30
    final_status = entry.get("status")
    filled = float(entry.get("filled") or 0.0)
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            refresh = exchange.fetch_order(entry_id, symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"  (poll) fetch_order: {exc}")
            continue
        final_status = refresh.get("status")
        filled = float(refresh.get("filled") or 0.0)
        print(f"  status={final_status} filled={filled}")
        if final_status in ("closed", "filled", "canceled", "rejected"):
            break

    if final_status not in ("closed", "filled"):
        # Cancel + abort.
        print(_color(f"FAIL: entry did not fill (final_status={final_status})", ok=False))
        if final_status == "open":
            try:
                exchange.cancel_order(entry_id, symbol)
                print("  cancelled the still-open entry order")
            except Exception as exc:  # noqa: BLE001
                print(_color(f"  cancel failed: {exc}", ok=False))
        return 8
    print(_color(f"OK  entry filled: filled={filled}"))

    # Place native stop.
    client_id_stop = f"mib-smoke-stop-{uuid.uuid4().hex[:8]}"
    print(f"\n[3] Placing STOP_MARKET sell {filled} {symbol} trigger={stop_price} (clientOrderId={client_id_stop}) ...")
    stop_id: str | None = None
    try:
        stop = exchange.create_order(
            symbol,
            "stop_market",
            "sell",
            filled,
            None,
            {
                "stopPrice": str(stop_price),
                "newClientOrderId": client_id_stop,
                # Spot doesn't honor reduceOnly, but futures does. Pass it
                # anyway — the testnet ignores unknown extras.
                "reduceOnly": True,
            },
        )
        stop_id = str(stop.get("id") or "")
        print(_color(f"OK  stop submitted: id={stop_id} status={stop.get('status')}"))
    except Exception as exc:  # noqa: BLE001
        # Spot testnet may not support stop_market for the BTC/USDT pair —
        # fall back to a plain stop_loss_limit which is widely supported.
        print(f"  stop_market not accepted ({exc}); retrying with STOP_LOSS_LIMIT ...")
        try:
            stop = exchange.create_order(
                symbol,
                "STOP_LOSS_LIMIT",
                "sell",
                filled,
                stop_price,
                {
                    "stopPrice": str(stop_price),
                    "newClientOrderId": client_id_stop,
                    "timeInForce": "GTC",
                },
            )
            stop_id = str(stop.get("id") or "")
            print(_color(f"OK  stop submitted (fallback STOP_LOSS_LIMIT): id={stop_id}"))
        except Exception as exc2:  # noqa: BLE001
            print(_color(f"FAIL: stop create failed twice: {exc2}", ok=False))
            return 9

    # Cleanup: cancel the stop so we don't leave a resting order.
    print("\n[4] Cleanup — cancelling the stop order ...")
    try:
        if stop_id:
            exchange.cancel_order(stop_id, symbol)
            print(_color(f"OK  cancelled stop id={stop_id}"))
    except Exception as exc:  # noqa: BLE001
        print(_color(f"WARN: stop cancel failed: {exc}", ok=False))

    print()
    print(_color("OK  full sandbox round-trip succeeded"))
    print(
        "    NOTE: the filled BTC stays in the testnet account. "
        "Manual cleanup via the testnet dashboard if you want to free it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
