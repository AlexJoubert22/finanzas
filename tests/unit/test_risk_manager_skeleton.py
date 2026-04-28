"""Tests for :class:`RiskManager`: gate orchestration and short-circuit."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from mib.models.portfolio import PortfolioSnapshot
from mib.trading.risk.manager import RiskManager
from mib.trading.risk.protocol import GateResult
from mib.trading.signals import PersistedSignal, Signal


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


def _persisted(signal_id: int = 42) -> PersistedSignal:
    return PersistedSignal(
        id=signal_id,
        status="pending",
        signal=_signal(),
        status_updated_at=datetime.now(UTC),
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[],
        positions=[],
        equity_quote=Decimal(0),
        last_synced_at=datetime.now(UTC),
        source="dry-run",
    )


class _StubGate:
    """Test gate that always returns the configured GateResult.

    Carries an instance-level ``name`` to satisfy the Gate Protocol
    (which only requires ``name: str`` to be accessible from the
    instance — class attribute or instance attribute both work).
    """

    def __init__(self, result: GateResult, *, name_override: str = "stub") -> None:
        self._result = result
        self.calls = 0
        self.name = name_override

    async def check(self, *_: Any, **__: Any) -> GateResult:
        self.calls += 1
        return self._result


class _RaisingGate:
    name: ClassVar[str] = "raises"

    async def check(self, *_: Any, **__: Any) -> GateResult:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_empty_gates_approves() -> None:
    manager = RiskManager(gates=[])
    decision = await manager.evaluate(_persisted(), _portfolio())
    assert decision.approved is True
    assert decision.gate_results == ()
    assert decision.signal_id == 42
    assert decision.version == 1
    assert decision.sized_amount is None


@pytest.mark.asyncio
async def test_single_passing_gate_approves() -> None:
    g = _StubGate(GateResult(True, "ok", "stub"))
    manager = RiskManager(gates=[g])
    decision = await manager.evaluate(_persisted(), _portfolio())
    assert decision.approved is True
    assert g.calls == 1


@pytest.mark.asyncio
async def test_failing_gate_rejects_and_short_circuits() -> None:
    g1 = _StubGate(
        GateResult(False, "blocked", "first"), name_override="first"
    )
    g2 = _StubGate(
        GateResult(True, "would-pass", "second"), name_override="second"
    )
    manager = RiskManager(gates=[g1, g2])
    decision = await manager.evaluate(_persisted(), _portfolio())
    assert decision.approved is False
    # Second gate never called — short-circuit invariant.
    assert g1.calls == 1
    assert g2.calls == 0
    # Reasoning mentions the rejecting gate.
    assert "first" in decision.reasoning


@pytest.mark.asyncio
async def test_all_passing_runs_every_gate_in_order() -> None:
    g1 = _StubGate(GateResult(True, "g1 pass", "first"), name_override="first")
    g2 = _StubGate(GateResult(True, "g2 pass", "second"), name_override="second")
    manager = RiskManager(gates=[g1, g2])
    decision = await manager.evaluate(_persisted(), _portfolio())
    assert decision.approved is True
    assert g1.calls == 1
    assert g2.calls == 1
    assert [r.gate_name for r in decision.gate_results] == ["first", "second"]


@pytest.mark.asyncio
async def test_gate_exception_propagates_no_decision_returned() -> None:
    """Atomicity: an unexpected gate failure must NOT yield a decision.

    Caller never gets a RiskDecision instance, so it never has anything
    to persist. Error context is preserved (the original RuntimeError).
    """
    g_bad = _RaisingGate()
    manager = RiskManager(gates=[g_bad])  # type: ignore[list-item]
    with pytest.raises(RuntimeError, match="boom"):
        await manager.evaluate(_persisted(), _portfolio())


@pytest.mark.asyncio
async def test_decision_records_explicit_version() -> None:
    g = _StubGate(GateResult(True, "ok", "stub"))
    manager = RiskManager(gates=[g])
    decision = await manager.evaluate(_persisted(), _portfolio(), version=7)
    assert decision.version == 7


def test_gates_property_is_immutable_view() -> None:
    g = _StubGate(GateResult(True, "ok", "stub"))
    manager = RiskManager(gates=[g])
    # Returns a tuple; mutating it doesn't affect manager state.
    gates_view = manager.gates
    assert isinstance(gates_view, tuple)


@pytest.mark.asyncio
async def test_decision_has_decided_at_timestamp() -> None:
    g = _StubGate(GateResult(True, "ok", "stub"))
    manager = RiskManager(gates=[g])
    before = datetime.now(UTC)
    decision = await manager.evaluate(_persisted(), _portfolio())
    after = datetime.now(UTC)
    assert before <= decision.decided_at <= after
