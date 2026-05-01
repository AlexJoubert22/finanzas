"""Two-step /go_live with email-2FA (FASE 14.2).

Flow:

1. ``/go_live <reason ≥30 chars>`` calls :meth:`GoLiveFlow.initiate`:
   - Runs preflight; if not ready, refuses immediately with the
     failed-checks list.
   - Generates a 6-digit code via :func:`secrets.randbelow`.
   - Hashes the code with sha256 + per-row salt (the pending_id),
     persists in ``go_live_pendings`` with TTL.
   - Emails the code to ``OPERATOR_EMAIL`` via SMTP. Refuses if
     SMTP isn't configured (no Telegram fallback — that defeats the
     "alternative channel" point of 2FA).
   - Returns a small InitiateResult with pending_id + ttl.

2. ``/go_live_confirm <code>``:
   - Looks up the most recent ``pending`` row for the actor.
   - Verifies (timing-safe) code, expiry, attempt count, min-delay.
   - On success: flips ``trading_state.enabled=True`` + ``mode=LIVE``
     via the existing :class:`ModeService` (which writes the
     ``mode_transitions`` audit row).
   - On failure: increments attempts, returns explicit reason.

The flow NEVER directly writes ``trading_state.enabled`` outside of
the :class:`TradingStateService`/:class:`ModeService` paths so the
audit trail in ``mode_transitions`` is the canonical record.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.config import Settings, get_settings
from mib.db.models import GoLivePendingRow
from mib.logger import logger
from mib.operations.preflight import PreflightReport, run_preflight
from mib.trading.mode import TradingMode
from mib.trading.mode_service import ModeService

#: Reason text must be at least this long to avoid drive-by /go_live.
MIN_REASON_LEN: int = 30


@dataclass(frozen=True)
class InitiateResult:
    """Outcome of step 1."""

    accepted: bool
    pending_id: str | None = None
    ttl_seconds: int = 0
    reason: str | None = None
    """Why rejected (preflight failed, smtp missing, reason too short)."""

    preflight: PreflightReport | None = None


@dataclass(frozen=True)
class ConfirmResult:
    """Outcome of step 2."""

    accepted: bool
    reason: str | None = None
    transition_id: int | None = None


class _SmtpUnavailableError(Exception):
    """SMTP not configured — refuse to send the 2FA code."""


EmailSender = Callable[..., Awaitable[None]]
PreflightRunner = Callable[[], Awaitable[PreflightReport]]


# ─── Email sender (injectable for tests) ────────────────────────────


async def _send_email_default(
    *, to: str, subject: str, body: str, settings: Settings
) -> None:
    """Default SMTP sender. Replaced by a stub in tests."""
    if (
        not settings.smtp_host
        or not settings.smtp_user
        or not settings.smtp_pass
        or not settings.smtp_from
        or not to
    ):
        raise _SmtpUnavailableError("SMTP settings or operator_email missing")
    import smtplib  # noqa: PLC0415
    from email.message import EmailMessage  # noqa: PLC0415

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_pass)
        smtp.send_message(msg)


# ─── GoLiveFlow ──────────────────────────────────────────────────────


class GoLiveFlow:
    """Stateful two-step /go_live coordinator."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        mode_service: ModeService,
        settings: Settings | None = None,
        email_sender: EmailSender = _send_email_default,
        preflight_runner: PreflightRunner = run_preflight,
    ) -> None:
        self._sf = session_factory
        self._mode = mode_service
        self._settings = settings or get_settings()
        self._email_sender = email_sender
        self._preflight = preflight_runner

    async def initiate(self, *, actor: str, reason: str) -> InitiateResult:
        cleaned = (reason or "").strip()
        if len(cleaned) < MIN_REASON_LEN:
            return InitiateResult(
                accepted=False,
                reason=(
                    f"reason_too_short:{len(cleaned)}_chars_need_{MIN_REASON_LEN}"
                ),
            )

        # 1) Preflight gate.
        report = await self._preflight()
        if not report.ready:
            return InitiateResult(
                accepted=False,
                reason="preflight_not_ready",
                preflight=report,
            )

        # 2) Generate code + persist hashed.
        code = f"{secrets.randbelow(1_000_000):06d}"
        pending_id = secrets.token_urlsafe(24)
        code_hash = _hash_code(code=code, pending_id=pending_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        ttl = self._settings.go_live_code_ttl_seconds
        expires_at = now + timedelta(seconds=ttl)

        async with self._sf() as session, session.begin():
            row = GoLivePendingRow(
                pending_id=pending_id,
                actor=actor,
                reason=cleaned,
                code_hash=code_hash,
                initiated_at=now,
                expires_at=expires_at,
                confirmed_at=None,
                attempts=0,
                status="pending",
            )
            session.add(row)

        # 3) Email the code via the injected sender.
        try:
            await self._email_sender(
                to=self._settings.operator_email,
                subject="MIB /go_live confirmation code",
                body=(
                    f"Code: {code}\n"
                    f"Valid for {ttl} seconds.\n"
                    f"Reply with: /go_live_confirm {code}\n\n"
                    f"Initiated by: {actor}\n"
                    f"Reason: {cleaned}"
                ),
                settings=self._settings,
            )
        except _SmtpUnavailableError as exc:
            # Roll back the pending row — without an email we can't
            # complete the 2FA cycle.
            await self._mark_status(pending_id, "rejected")
            return InitiateResult(
                accepted=False,
                reason=f"smtp_unavailable:{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._mark_status(pending_id, "rejected")
            logger.error("go_live: email send failed: {}", exc)
            return InitiateResult(
                accepted=False,
                reason=f"email_send_failed:{exc.__class__.__name__}",
            )

        logger.info(
            "go_live: initiated pending_id={} actor={} ttl={}",
            pending_id, actor, ttl,
        )
        return InitiateResult(
            accepted=True,
            pending_id=pending_id,
            ttl_seconds=ttl,
            preflight=report,
        )

    async def confirm(self, *, actor: str, code: str) -> ConfirmResult:
        """Look up the most-recent pending row for ``actor`` + verify code."""
        cleaned_code = (code or "").strip()
        async with self._sf() as session:
            stmt = (
                select(GoLivePendingRow)
                .where(
                    GoLivePendingRow.actor == actor,
                    GoLivePendingRow.status == "pending",
                )
                .order_by(GoLivePendingRow.initiated_at.desc())
                .limit(1)
            )
            row = (await session.scalars(stmt)).first()
        if row is None:
            return ConfirmResult(
                accepted=False,
                reason="no_pending_for_actor",
            )

        now = datetime.now(UTC).replace(tzinfo=None)
        # Min delay anti-rapid-fire.
        elapsed = (now - row.initiated_at).total_seconds()
        if elapsed < self._settings.go_live_min_confirm_delay_seconds:
            return ConfirmResult(
                accepted=False,
                reason=(
                    f"rate_limit:{elapsed:.0f}s_need_"
                    f"{self._settings.go_live_min_confirm_delay_seconds}s"
                ),
            )
        if now >= row.expires_at:
            await self._mark_status(row.pending_id, "expired")
            return ConfirmResult(
                accepted=False, reason="expired"
            )
        if row.attempts >= self._settings.go_live_max_attempts:
            await self._mark_status(row.pending_id, "rejected")
            return ConfirmResult(
                accepted=False, reason="too_many_attempts"
            )

        expected = row.code_hash
        candidate = _hash_code(code=cleaned_code, pending_id=row.pending_id)
        if not secrets.compare_digest(expected, candidate):
            await self._increment_attempts(row.pending_id)
            return ConfirmResult(accepted=False, reason="bad_code")

        # ✅ Verified. Flip mode -> LIVE via the canonical ModeService
        # (writes the mode_transitions audit row + flips trading_state).
        result = await self._mode.transition_to(
            TradingMode.LIVE,
            actor=actor,
            reason=f"/go_live confirmed: {row.reason[:200]}",
            force=True,  # /go_live is the explicit operator-issued LIVE entry
        )
        if not result.allowed:
            return ConfirmResult(
                accepted=False,
                reason=f"mode_transition_failed:{result.reason}",
            )

        # Also flip trading_state.enabled=True alongside the mode flip.
        try:
            from mib.trading.risk.state import (  # noqa: PLC0415
                TradingStateService,
            )

            await TradingStateService(self._sf).update(
                actor=f"go_live:{actor}", enabled=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("go_live: enabled flip failed: {}", exc)
            return ConfirmResult(
                accepted=False,
                reason=f"enabled_flip_failed:{exc.__class__.__name__}",
            )

        await self._mark_confirmed(row.pending_id, confirmed_at=now)
        logger.warning(
            "go_live: CONFIRMED pending_id={} actor={} transition_id={}",
            row.pending_id, actor, result.transition_id,
        )
        return ConfirmResult(
            accepted=True,
            transition_id=result.transition_id,
        )

    # ─── Internal ─────────────────────────────────────────────────

    async def _mark_status(
        self,
        pending_id: str,
        status: Literal["pending", "confirmed", "expired", "rejected"],
    ) -> None:
        async with self._sf() as session, session.begin():
            stmt = select(GoLivePendingRow).where(
                GoLivePendingRow.pending_id == pending_id
            )
            row = (await session.scalars(stmt)).first()
            if row is not None:
                row.status = status

    async def _increment_attempts(self, pending_id: str) -> None:
        async with self._sf() as session, session.begin():
            stmt = select(GoLivePendingRow).where(
                GoLivePendingRow.pending_id == pending_id
            )
            row = (await session.scalars(stmt)).first()
            if row is not None:
                row.attempts = row.attempts + 1

    async def _mark_confirmed(
        self, pending_id: str, *, confirmed_at: datetime
    ) -> None:
        async with self._sf() as session, session.begin():
            stmt = select(GoLivePendingRow).where(
                GoLivePendingRow.pending_id == pending_id
            )
            row = (await session.scalars(stmt)).first()
            if row is None:
                return
            row.confirmed_at = confirmed_at
            row.status = "confirmed"


# ─── Pure helpers ────────────────────────────────────────────────────


def _hash_code(*, code: str, pending_id: str) -> str:
    """SHA-256 of ``pending_id|code`` — per-row salting."""
    return hashlib.sha256(f"{pending_id}|{code}".encode()).hexdigest()
