"""Tests for the Telegram callback handlers' append-only contract.

Each callback (✅, ❌) must write a row to ``signal_status_events``
with ``actor=f"user:{telegram_id}"``, NOT just flip ``signals.status``.
This guarantees the audit trail tells us WHO approved or cancelled
each signal.

The handlers also handle the race where the signal is no longer
``pending`` (e.g. expired by the TTL job between message dispatch
and the user clicking) — they must NOT write a transition event in
that case, instead show a clear "no longer pending" message.

FASE 8.6 added a re-evaluation step before the consume transition.
We mock the risk dependencies so this test stays focused on the
callback's transition contract; the risk-wiring path is exercised
in :mod:`tests.unit.test_signal_notify` and the integration test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from mib.api.dependencies import get_signal_repository
from mib.models.portfolio import PortfolioSnapshot
from mib.telegram.handlers import signals as handlers_mod
from mib.telegram.handlers.signals import _cancel_signal, _consume_signal
from mib.trading.risk.decision import RiskDecision
from mib.trading.signals import PersistedSignal, Signal


@pytest.fixture(autouse=True)
def _mock_risk_in_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the risk dependencies used by the consume callback.

    Returns a fresh approved RiskDecision on demand so the callback's
    re-evaluation path proceeds without needing trading_state seeded
    or a real portfolio snapshot.
    """

    class _FakePortfolio:
        async def snapshot(self) -> PortfolioSnapshot:
            return PortfolioSnapshot(
                balances=[],
                positions=[],
                equity_quote=Decimal(0),
                last_synced_at=datetime.now(UTC),
                source="dry-run",
            )

    class _FakeRiskManager:
        async def evaluate(
            self, persisted: PersistedSignal, portfolio: Any, *, version: int = 1  # noqa: ARG002
        ) -> RiskDecision:
            return RiskDecision(
                signal_id=persisted.id,
                version=version,
                approved=True,
                gate_results=(),
                reasoning="approved by fake",
                decided_at=datetime.now(UTC),
                sized_amount=Decimal("100"),
            )

    class _FakeDecisionRepo:
        async def latest_for_signal(self, _id: int) -> RiskDecision | None:
            return None  # force re-evaluation each call

        async def append_with_retry(
            self, _id: int, factory: Any, *, max_retries: int = 3  # noqa: ARG002
        ) -> RiskDecision:
            return factory(1)

    monkeypatch.setattr(handlers_mod, "get_portfolio_state", lambda: _FakePortfolio())
    monkeypatch.setattr(handlers_mod, "get_risk_manager", lambda: _FakeRiskManager())
    monkeypatch.setattr(
        handlers_mod, "get_risk_decision_repository", lambda: _FakeDecisionRepo()
    )


def _signal() -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _fake_update(*, telegram_id: int = 42) -> Any:
    """Minimal Update spoof for handler tests.

    Real PTB Updates carry a lot of fields the handlers don't touch.
    We only mock what's read (effective_user.id) and what's awaited
    (callback_query.edit_message_text).
    """
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = telegram_id
    update.callback_query = MagicMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_consume_callback_writes_event_with_user_actor(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_signal_repository()
    persisted = await repo.add(_signal())

    update = _fake_update(telegram_id=42)
    await _consume_signal(update, persisted.id)

    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.status == "consumed"

    events = await repo.list_events(persisted.id)
    transition_event = events[-1]
    assert transition_event.event_type == "approved"
    assert transition_event.actor == "user:42"
    assert transition_event.from_status == "pending"
    assert transition_event.to_status == "consumed"


@pytest.mark.asyncio
async def test_cancel_callback_writes_event_with_user_actor(
    fresh_db: None,  # noqa: ARG001
) -> None:
    repo = get_signal_repository()
    persisted = await repo.add(_signal())

    update = _fake_update(telegram_id=99)
    await _cancel_signal(update, persisted.id)

    refreshed = await repo.get(persisted.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"

    events = await repo.list_events(persisted.id)
    transition_event = events[-1]
    assert transition_event.event_type == "cancelled"
    assert transition_event.actor == "user:99"


@pytest.mark.asyncio
async def test_consume_on_already_expired_signal_shows_message(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """Race: TTL job expired the signal before the user clicked ✅.

    The callback should refuse gracefully (no new transition event)
    and tell the user via edit_message_text.
    """
    repo = get_signal_repository()
    persisted = await repo.add(_signal())
    # Simulate the TTL job winning the race.
    await repo.transition(
        persisted.id,
        "expired",
        actor="job:expire_stale_signals",
        event_type="expired",
    )

    update = _fake_update(telegram_id=42)
    await _consume_signal(update, persisted.id)

    # User got a message (mocked AsyncMock recorded the call).
    assert update.callback_query.edit_message_text.await_count == 1
    call_args = update.callback_query.edit_message_text.await_args
    message = call_args[0][0] if call_args[0] else call_args[1].get("text", "")
    assert "no" in message.lower()  # "ya no está pendiente" / "no encontrada"

    # Status remains 'expired'; no new approval event was written.
    events = await repo.list_events(persisted.id)
    # 'created' + 'expired' only — no 'approved'.
    assert [e.event_type for e in events] == ["created", "expired"]


@pytest.mark.asyncio
async def test_consume_on_unknown_signal_id_shows_not_found(
    fresh_db: None,  # noqa: ARG001
) -> None:
    update = _fake_update(telegram_id=42)
    await _consume_signal(update, 99_999)
    assert update.callback_query.edit_message_text.await_count == 1
