"""Tests for the /go_live two-step flow (FASE 14.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from mib.config import get_settings
from mib.db.models import GoLivePendingRow
from mib.db.session import async_session_factory
from mib.observability.scheduler_health import (
    _reset_for_tests as _reset_health,
)
from mib.operations.go_live import (
    GoLiveFlow,
    InitiateResult,
    PreflightReport,
    _hash_code,
)
from mib.operations.preflight import CheckResult
from mib.trading.mode import TradingMode
from mib.trading.mode_service import ModeService
from mib.trading.mode_transitions_repo import ModeTransitionRepository
from mib.trading.risk.state import TradingStateService


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    _reset_health()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_state() -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO trading_state "
                "(id, enabled, daily_dd_max_pct, total_dd_max_pct, "
                " killed_until, mode, last_modified_at, last_modified_by) "
                "VALUES (1, 0, 0.03, 0.25, NULL, 'paper', "
                "CURRENT_TIMESTAMP, 'test')"
            )
        )


def _mode_service() -> ModeService:
    return ModeService(
        session_factory=async_session_factory,
        state_service=TradingStateService(async_session_factory),
        transitions_repo=ModeTransitionRepository(async_session_factory),
    )


async def _ready_preflight() -> PreflightReport:
    return PreflightReport(
        ran_at=_now(),
        checks=[
            CheckResult(name="trading_state", passed=True, details="ok"),
        ],
    )


async def _not_ready_preflight() -> PreflightReport:
    return PreflightReport(
        ran_at=_now(),
        checks=[
            CheckResult(
                name="dead_man",
                passed=False,
                details="HEARTBEAT_TOKEN empty",
                severity="critical",
            ),
        ],
    )


# ─── Pure helper ────────────────────────────────────────────────────


def test_hash_code_uses_pending_id_as_salt() -> None:
    """Same code with different pending_id → different hash."""
    a = _hash_code(code="123456", pending_id="abc")
    b = _hash_code(code="123456", pending_id="def")
    assert a != b
    # Stable.
    assert a == _hash_code(code="123456", pending_id="abc")


# ─── initiate() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initiate_short_reason_rejected(
    fresh_db: None,  # noqa: ARG001
) -> None:
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    result = await flow.initiate(actor="user:1", reason="too short")
    assert result.accepted is False
    assert result.reason is not None
    assert "reason_too_short" in result.reason


@pytest.mark.asyncio
async def test_initiate_preflight_not_ready_rejected(
    fresh_db: None,  # noqa: ARG001
) -> None:
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_not_ready_preflight,
    )
    result = await flow.initiate(
        actor="user:1",
        reason="x" * 50,  # long enough
    )
    assert result.accepted is False
    assert result.reason == "preflight_not_ready"
    assert result.preflight is not None
    assert not result.preflight.ready


@pytest.mark.asyncio
async def test_initiate_smtp_unavailable_rejected(
    fresh_db: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default SMTP sender raises when settings are empty."""
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "operator_email", "")

    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    result = await flow.initiate(
        actor="user:1",
        reason=("validating live readiness from a 35d PAPER run with 52 "
                "closed trades and clean streak intact"),
    )
    assert result.accepted is False
    assert result.reason is not None
    assert "smtp_unavailable" in result.reason


@pytest.mark.asyncio
async def test_initiate_success_persists_pending_row_with_hashed_code(
    fresh_db: None,  # noqa: ARG001
) -> None:
    sent: dict[str, Any] = {}

    async def _stub_sender(*, to: str, subject: str, body: str, settings: Any) -> None:
        sent.update({"to": to, "subject": subject, "body": body})

    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        email_sender=_stub_sender,
        preflight_runner=_ready_preflight,
    )
    reason = (
        "validating live readiness from a 35d PAPER run with 52 closed "
        "trades and clean streak intact"
    )
    result = await flow.initiate(actor="user:1", reason=reason)
    assert result.accepted is True
    assert result.pending_id is not None
    # Email was invoked.
    assert "Code:" in sent["body"]
    # Persisted with hashed code (NOT plaintext).
    async with async_session_factory() as session:
        stmt = select(GoLivePendingRow).where(
            GoLivePendingRow.pending_id == result.pending_id
        )
        row = (await session.scalars(stmt)).first()
    assert row is not None
    assert row.actor == "user:1"
    assert row.reason == reason.strip()
    assert len(row.code_hash) == 64  # sha256 hex
    assert row.status == "pending"
    assert row.attempts == 0


# ─── confirm() ───────────────────────────────────────────────────────


async def _initiate_with_known_code(
    flow: GoLiveFlow, code: str = "123456"
) -> str:
    """Seed a pending row by directly inserting (we can't intercept the
    code from the random generator; this test fixture writes its own
    so confirm() can verify it).
    """
    pending_id = "test-pending-1"
    code_hash = _hash_code(code=code, pending_id=pending_id)
    now = _now()
    async with async_session_factory() as session, session.begin():
        session.add(
            GoLivePendingRow(
                pending_id=pending_id,
                actor="user:1",
                reason="x" * 50,
                code_hash=code_hash,
                initiated_at=now - timedelta(seconds=60),
                expires_at=now + timedelta(seconds=240),
                attempts=0,
                status="pending",
            )
        )
    return pending_id


@pytest.mark.asyncio
async def test_confirm_success_flips_state_and_mode(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    await _initiate_with_known_code(flow, code="654321")
    result = await flow.confirm(actor="user:1", code="654321")
    assert result.accepted is True
    assert result.transition_id is not None
    # trading_state flipped.
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is True
    assert state.mode == TradingMode.LIVE.value


@pytest.mark.asyncio
async def test_confirm_bad_code_increments_attempts(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    pending_id = await _initiate_with_known_code(flow, code="111111")
    r = await flow.confirm(actor="user:1", code="999999")
    assert r.accepted is False
    assert r.reason == "bad_code"
    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(GoLivePendingRow).where(
                    GoLivePendingRow.pending_id == pending_id
                )
            )
        ).first()
    assert row is not None
    assert row.attempts == 1
    # State NOT flipped.
    state = await TradingStateService(async_session_factory).get()
    assert state.enabled is False


@pytest.mark.asyncio
async def test_confirm_rate_limit_under_30s(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    # Insert with initiated_at = now (under 30s ago).
    pending_id = "fresh-pending"
    code = "246810"
    code_hash = _hash_code(code=code, pending_id=pending_id)
    now = _now()
    async with async_session_factory() as session, session.begin():
        session.add(
            GoLivePendingRow(
                pending_id=pending_id,
                actor="user:1",
                reason="x" * 50,
                code_hash=code_hash,
                initiated_at=now,
                expires_at=now + timedelta(minutes=5),
                attempts=0,
                status="pending",
            )
        )
    r = await flow.confirm(actor="user:1", code=code)
    assert r.accepted is False
    assert r.reason is not None
    assert "rate_limit" in r.reason


@pytest.mark.asyncio
async def test_confirm_expired_marks_status(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    pending_id = "expired-pending"
    code = "121212"
    code_hash = _hash_code(code=code, pending_id=pending_id)
    now = _now()
    async with async_session_factory() as session, session.begin():
        session.add(
            GoLivePendingRow(
                pending_id=pending_id,
                actor="user:1",
                reason="x" * 50,
                code_hash=code_hash,
                initiated_at=now - timedelta(minutes=10),
                expires_at=now - timedelta(seconds=1),
                attempts=0,
                status="pending",
            )
        )
    r = await flow.confirm(actor="user:1", code=code)
    assert r.accepted is False
    assert r.reason == "expired"
    async with async_session_factory() as session:
        row = (
            await session.scalars(
                select(GoLivePendingRow).where(
                    GoLivePendingRow.pending_id == pending_id
                )
            )
        ).first()
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_confirm_too_many_attempts_blocked(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    pending_id = "max-attempts"
    code = "333333"
    code_hash = _hash_code(code=code, pending_id=pending_id)
    now = _now()
    settings = get_settings()
    async with async_session_factory() as session, session.begin():
        session.add(
            GoLivePendingRow(
                pending_id=pending_id,
                actor="user:1",
                reason="x" * 50,
                code_hash=code_hash,
                initiated_at=now - timedelta(seconds=60),
                expires_at=now + timedelta(seconds=240),
                attempts=settings.go_live_max_attempts,
                status="pending",
            )
        )
    r = await flow.confirm(actor="user:1", code=code)
    assert r.accepted is False
    assert r.reason == "too_many_attempts"


@pytest.mark.asyncio
async def test_confirm_no_pending_for_actor(
    fresh_db: None,  # noqa: ARG001
) -> None:
    await _seed_state()
    flow = GoLiveFlow(
        session_factory=async_session_factory,
        mode_service=_mode_service(),
        preflight_runner=_ready_preflight,
    )
    r = await flow.confirm(actor="user:99", code="000000")
    assert r.accepted is False
    assert r.reason == "no_pending_for_actor"


# Suppress unused-import warning for InitiateResult (re-exported for
# downstream test harnesses).
_ = InitiateResult
