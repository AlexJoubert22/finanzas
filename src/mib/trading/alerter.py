"""Lightweight alerter abstraction so the executor doesn't import the
PTB :class:`Application` directly.

Two reasons to keep this thin:

1. Tests pass an in-memory recorder; production binds to the running
   bot. Decoupling means tests don't construct a real Application.
2. The alerter is the only "side effect on the operator" surface
   from inside trading code. Keeping the interface small makes it
   easy to add Slack / email / SMS later without touching callers.

Failure mode: if Telegram is unreachable, ``alert`` logs a structlog
WARNING and returns. NEVER raises — the alerter is best-effort.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mib.config import get_settings
from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from mib.telegram import BotApp


@runtime_checkable
class TelegramAlerter(Protocol):
    """Minimal alert interface."""

    async def alert(self, text: str, *, parse_mode: str = "HTML") -> None: ...


class TelegramBotAlerter:
    """Sends alerts to the operator's whitelisted Telegram chat.

    Picks the FIRST id from ``settings.telegram_allowed_user_ids`` as
    the destination (admin). When the bot is disabled (no token), the
    alerter logs at INFO and returns — useful in test/dev/SHADOW.
    """

    def __init__(self, bot_app: BotApp | None) -> None:
        self._bot_app = bot_app
        self._chat_id: int | None = None
        ids = get_settings().telegram_allowed_user_ids
        if ids:
            # Lowest id deterministically — typically the operator
            # who configured the allowlist first.
            self._chat_id = sorted(ids)[0]

    async def alert(self, text: str, *, parse_mode: str = "HTML") -> None:
        if self._bot_app is None or self._chat_id is None:
            logger.info("alerter: bot disabled, would send: {}", text)
            return
        try:
            await self._bot_app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception as exc:  # noqa: BLE001 — never raise
            logger.warning(
                "alerter: send failed: {} (text={!r})", exc, text[:120]
            )


class NullAlerter:
    """No-op implementation. Used when the trading layer is exercised
    without a Telegram bot wired (CLI scripts, headless tests).

    Records alerts in-memory under ``recorded`` so smoke tests can
    assert that an alert *would have* been sent.
    """

    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def alert(self, text: str, *, parse_mode: str = "HTML") -> None:  # noqa: ARG002
        self.recorded.append(text)
        logger.info("null-alerter: recorded alert: {}", text)
