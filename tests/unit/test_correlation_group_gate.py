"""Tests for :class:`CorrelationGroupGate` and :class:`CorrelationGroups`."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mib.config import get_settings
from mib.db.session import async_session_factory
from mib.models.portfolio import Balance, PortfolioSnapshot, Position
from mib.trading.risk.correlation_groups import (
    CorrelationGroup,
    CorrelationGroups,
    CorrelationGroupsConfigError,
)
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.gates.correlation_group import CorrelationGroupGate
from mib.trading.risk.repo import RiskDecisionRepository
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal(ticker: str = "BTC/USDT") -> Signal:
    return Signal(
        ticker=ticker,
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(60_000.0, 60_010.0),
        invalidation=58_800.0,
        target_1=61_200.0,
        target_2=63_600.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 800.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _portfolio(positions: list[Position], equity: Decimal = Decimal("10000")) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        balances=[Balance(asset="EUR", free=equity, used=Decimal(0), total=equity)],
        positions=positions,
        equity_quote=equity,
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


def _position(symbol: str, amount: Decimal, mark: Decimal) -> Position:
    return Position(
        symbol=symbol,
        side="long",
        amount=amount,
        entry_price=mark,
        mark_price=mark,
        unrealized_pnl=Decimal(0),
        leverage=1.0,
    )


def _crypto_majors_groups() -> CorrelationGroups:
    return CorrelationGroups(
        [
            CorrelationGroup(
                name="crypto_majors",
                members=frozenset({"BTC/USDT", "ETH/USDT"}),
                max_pct=0.30,
            ),
        ]
    )


def _gate(groups: CorrelationGroups) -> CorrelationGroupGate:
    return CorrelationGroupGate(
        groups,
        SignalRepository(async_session_factory),
        RiskDecisionRepository(async_session_factory),
    )


# ─── CorrelationGroups (loader) ─────────────────────────────────────

def test_loader_reads_canonical_yaml() -> None:
    """The shipped config/correlation_groups.yaml must parse cleanly."""
    cfg = CorrelationGroups.from_yaml(Path("config/correlation_groups.yaml"))
    names = {g.name for g in cfg.all_groups}
    assert names == {"crypto_majors", "crypto_l1_l2", "us_megacap_tech", "us_indices"}


def test_loader_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CorrelationGroupsConfigError, match="not found"):
        CorrelationGroups.from_yaml(tmp_path / "ghost.yaml")


def test_loader_raises_on_bad_cap(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("group:\n  members: [X]\n  group_max_pct: 1.5\n", encoding="utf-8")
    with pytest.raises(CorrelationGroupsConfigError, match="group_max_pct"):
        CorrelationGroups.from_yaml(p)


def test_loader_raises_on_empty_members(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("group:\n  members: []\n  group_max_pct: 0.3\n", encoding="utf-8")
    with pytest.raises(CorrelationGroupsConfigError, match="non-empty list"):
        CorrelationGroups.from_yaml(p)


def test_groups_for_ticker_returns_matches() -> None:
    cfg = _crypto_majors_groups()
    matches = cfg.groups_for_ticker("BTC/USDT")
    assert len(matches) == 1
    assert matches[0].name == "crypto_majors"


def test_groups_for_ticker_returns_empty_for_unknown() -> None:
    cfg = _crypto_majors_groups()
    assert cfg.groups_for_ticker("XYZ/USDT") == []


# ─── Gate behaviour ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_passes_when_zero_equity(fresh_db: None) -> None:  # noqa: ARG001
    pf = PortfolioSnapshot(
        balances=[],
        positions=[],
        equity_quote=Decimal(0),
        last_synced_at=datetime.now(UTC),
        source="dry-run",
    )
    result = await _gate(_crypto_majors_groups()).check(_signal(), pf, get_settings())
    assert result.passed is True
    assert "no equity" in result.reason


@pytest.mark.asyncio
async def test_passes_when_ticker_in_no_group(fresh_db: None) -> None:  # noqa: ARG001
    pf = _portfolio(positions=[])
    result = await _gate(_crypto_majors_groups()).check(
        _signal("XYZ/USDT"), pf, get_settings()
    )
    assert result.passed is True
    assert "not in any correlation group" in result.reason


@pytest.mark.asyncio
async def test_rejects_when_combined_group_exposure_breaches(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """0.05 BTC at 50k = 2500, 1 ETH at 3000 = 3000, sum 5500. Equity
    10k, group cap 30% = 3000. 5500 >= 3000 → reject.
    """
    pf = _portfolio(
        [
            _position("BTC/USDT", Decimal("0.05"), Decimal("50000")),
            _position("ETH/USDT", Decimal("1"), Decimal("3000")),
        ]
    )
    result = await _gate(_crypto_majors_groups()).check(_signal(), pf, get_settings())
    assert result.passed is False
    assert "crypto_majors" in result.reason
    assert ">= cap" in result.reason


@pytest.mark.asyncio
async def test_passes_when_combined_below_cap(fresh_db: None) -> None:  # noqa: ARG001
    pf = _portfolio(
        [
            _position("BTC/USDT", Decimal("0.01"), Decimal("50000")),  # 500
            _position("ETH/USDT", Decimal("0.5"), Decimal("3000")),    # 1500
        ]
    )
    # 500 + 1500 = 2000 < 3000 cap.
    result = await _gate(_crypto_majors_groups()).check(_signal(), pf, get_settings())
    assert result.passed is True


@pytest.mark.asyncio
async def test_strictest_cap_wins_when_in_multiple_groups(
    fresh_db: None,  # noqa: ARG001
) -> None:
    """ticker in two groups; the gate iterates and the first failing
    group rejects. With caps 0.30 and 0.10, the 0.10 group rejects
    earlier even if the 0.30 group would let through.
    """
    groups = CorrelationGroups(
        [
            CorrelationGroup(
                name="loose", members=frozenset({"BTC/USDT"}), max_pct=0.30
            ),
            CorrelationGroup(
                name="strict", members=frozenset({"BTC/USDT"}), max_pct=0.10
            ),
        ]
    )
    pf = _portfolio(
        [_position("BTC/USDT", Decimal("0.05"), Decimal("50000"))],  # 2500
    )
    # 2500 vs 1000 (strict cap) → strict rejects. 2500 vs 3000 (loose) → loose passes.
    result = await _gate(groups).check(_signal(), pf, get_settings())
    assert result.passed is False
    # The reason mentions either group; with stable iteration order
    # (insertion preserved), 'loose' is checked first. 2500 < 3000, passes.
    # Then 'strict' is checked, 2500 >= 1000, rejects.
    assert "strict" in result.reason


@pytest.mark.asyncio
async def test_includes_sized_pending_in_combined_exposure(
    fresh_db: None,  # noqa: ARG001
) -> None:
    s_repo = SignalRepository(async_session_factory)
    d_repo = RiskDecisionRepository(async_session_factory)

    persisted = await s_repo.add(_signal("ETH/USDT"))
    await s_repo.transition(
        persisted.id, "consumed", actor="user:test", event_type="approved"
    )
    big_decision = RiskDecision(
        signal_id=persisted.id,
        version=1,
        approved=True,
        gate_results=(),
        reasoning="test",
        decided_at=datetime.now(UTC),
        sized_amount=Decimal("4000"),
    )
    await d_repo.add(big_decision)

    pf = _portfolio(positions=[])  # no realized exposure
    # 4000 sized pending on ETH alone exceeds the 3000 (30% × 10k) cap.
    result = await _gate(_crypto_majors_groups()).check(
        _signal("BTC/USDT"), pf, get_settings()
    )
    assert result.passed is False


def test_gate_name_is_class_attribute() -> None:
    assert CorrelationGroupGate.name == "correlation_group"
