"""Trading mode ladder.

The bot graduates through these modes one step at a time. Skipping a
step (e.g. jumping from SHADOW straight to LIVE) is explicitly not
supported by the design — the RiskManager and OrderExecutor read the
current mode and behave differently per step:

- ``OFF``       — no signals are even emitted.
- ``SHADOW``    — signals are generated and persisted; no exchange call.
- ``PAPER``     — orders go to the exchange's testnet sandbox.
- ``SEMI_AUTO`` — orders are proposed via Telegram; human approves.
- ``LIVE``      — fully automated execution against real funds.

Configured via the ``TRADING_MODE`` env var and switchable at runtime
through the future ``/mode`` Telegram command.
"""

from __future__ import annotations

from enum import StrEnum


class TradingMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    PAPER = "paper"
    SEMI_AUTO = "semi_auto"
    LIVE = "live"
