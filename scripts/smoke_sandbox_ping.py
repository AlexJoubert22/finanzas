"""Smoke-test the Binance sandbox credentials.

Reads ``BINANCE_SANDBOX_API_KEY`` / ``BINANCE_SANDBOX_SECRET`` from
``.env``, performs an authenticated ``fetch_balance`` against
``testnet.binance.vision``, and prints the non-zero balances. A green
output proves the keys + endpoint + outbound network are all working.

Uses the synchronous ``ccxt`` client on purpose — the production
trader is async (aiohttp), but some constrained shells fail aiohttp
DNS resolution while the sync ``requests`` stack works fine. The bot
itself runs in an environment where async DNS works; this script
tolerates both.

Run::

    PYTHONUTF8=1 uv run python scripts/smoke_sandbox_ping.py
"""

from __future__ import annotations

import sys

from dotenv import dotenv_values


def _color(s: str, ok: bool = True) -> str:
    code = "\033[32m" if ok else "\033[31m"
    return f"{code}{s}\033[0m"


def main() -> int:
    env = dotenv_values(".env")
    api_key = (env.get("BINANCE_SANDBOX_API_KEY") or "").strip()
    secret = (env.get("BINANCE_SANDBOX_SECRET") or "").strip()
    base_url = env.get("BINANCE_SANDBOX_BASE_URL") or "https://testnet.binance.vision"

    if not api_key or not secret:
        print(_color("FAIL: BINANCE_SANDBOX_API_KEY/SECRET missing in .env", ok=False))
        print("See docs/SANDBOX-SETUP.md for instructions.")
        return 2
    if "testnet" not in base_url.lower() and "sandbox" not in base_url.lower():
        print(
            _color(
                f"FAIL: base_url {base_url!r} does not contain "
                "'testnet' or 'sandbox' — third seatbelt would block writes.",
                ok=False,
            )
        )
        return 3

    print(f"Connecting to {base_url} ...")

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

    try:
        balance = exchange.fetch_balance()
    except Exception as exc:  # noqa: BLE001
        print(_color(f"FAIL: fetch_balance raised {type(exc).__name__}", ok=False))
        print(f"  {exc}")
        if "Invalid API-key" in str(exc) or "Signature" in str(exc):
            print(
                "  Likely cause: stray whitespace in the key. Re-copy from "
                "the testnet dashboard, ensure no trailing space."
            )
        return 5

    nonzero = {k: v for k, v in balance.get("total", {}).items() if v}
    print(_color(f"OK  authenticated against {base_url}"))
    print(f"  total assets in response: {len(balance.get('total', {}))}")
    print(f"  non-zero balances ({len(nonzero)}):")
    for asset, amount in sorted(nonzero.items())[:15]:
        print(f"    {asset}: {amount}")
    if len(nonzero) > 15:
        print(f"    ... and {len(nonzero) - 15} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
