"""Tests for :class:`PositionSizer` and the cap chain."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mib.config import get_settings
from mib.models.portfolio import Balance, PortfolioSnapshot
from mib.trading.signals import Signal
from mib.trading.sizing import PositionSizer


def _signal(
    *,
    entry: float = 100.0,
    invalidation: float = 97.0,
    target_1: float = 103.0,
    target_2: float | None = 109.0,
) -> Signal:
    return Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(entry, entry),
        invalidation=invalidation,
        target_1=target_1,
        target_2=target_2,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )


def _portfolio(
    *,
    equity: Decimal = Decimal("10000"),
    free_eur: Decimal | None = None,
) -> PortfolioSnapshot:
    free = equity if free_eur is None else free_eur
    return PortfolioSnapshot(
        balances=[Balance(asset="EUR", free=free, used=Decimal(0), total=equity)],
        positions=[],
        equity_quote=equity,
        last_synced_at=datetime.now(UTC),
        source="exchange",
    )


def test_standard_signal_produces_risk_pct_of_equity() -> None:
    """Risk per trade in EUR is exactly risk_per_trade_pct × equity,
    regardless of stop distance.

    With defaults: risk_per_trade=0.5%, equity=10000 → risk=50 EUR.
    Stop distance = entry - invalidation = 100 - 97 = 3.
    size_units = 50 / 3 ≈ 16.67. size_quote = 16.67 × 100 ≈ 1666.67.
    Capped at max_position_pct=10% × 10000 = 1000.
    """
    sizer = PositionSizer()
    result = sizer.size(_signal(), _portfolio(), get_settings())
    assert result.amount > 0
    # max_position_pct cap should fire (1666.67 > 1000)
    assert "max_position_pct" in result.caps_applied
    assert result.amount == Decimal("1000")


def test_distant_stop_produces_smaller_size() -> None:
    """Same equity + risk %, but a stop 30 away → tiny position.
    size_units = 50/30 ≈ 1.67. size_quote ≈ 167. Below max_position
    cap so no cap fires.
    """
    sizer = PositionSizer()
    sig = _signal(entry=100.0, invalidation=70.0, target_1=130.0, target_2=190.0)
    result = sizer.size(sig, _portfolio(), get_settings())
    assert result.amount > 0
    assert result.caps_applied == ()
    # Approximately 50/30 × 100 = 166.6...
    assert Decimal("160") < result.amount < Decimal("170")


def test_tight_stop_caps_at_max_position() -> None:
    """Very tight stop (0.5 away) → huge size, capped at max_position."""
    sizer = PositionSizer()
    sig = _signal(entry=100.0, invalidation=99.5, target_1=101.0, target_2=102.5)
    result = sizer.size(sig, _portfolio(), get_settings())
    assert "max_position_pct" in result.caps_applied
    assert result.amount == Decimal("1000")  # 10% of 10000


def test_available_cash_caps_below_position_cap() -> None:
    """Free cash 500 < max_position 1000 → cap at 500."""
    sizer = PositionSizer()
    pf = _portfolio(equity=Decimal("10000"), free_eur=Decimal("500"))
    result = sizer.size(_signal(), pf, get_settings())
    assert "available_cash" in result.caps_applied
    assert result.amount == Decimal("500")


def test_min_notional_returns_zero_with_reason() -> None:
    """Tiny equity makes the natural size below min_notional 10.

    equity=100, risk=0.5% → 0.5 EUR risk. Distance 3. size_units 0.167.
    size_quote = 16.67. max_position_pct=10% × 100 = 10 → capped 10.
    Available cash 100 → no cash cap. Then 10 == min_notional, exactly
    at the line — must be > 0. Make equity smaller: equity=50.
    risk=0.25; size_units=0.083; size_quote=8.33. max_pos=5. cap to 5.
    5 < min_notional 10 → return 0.
    """
    sizer = PositionSizer()
    pf = _portfolio(equity=Decimal("50"))
    result = sizer.size(_signal(), pf, get_settings())
    assert result.amount == Decimal(0)
    assert "min_notional" in result.caps_applied
    assert "min_notional" in result.reasoning


def test_zero_equity_returns_zero() -> None:
    sizer = PositionSizer()
    pf = _portfolio(equity=Decimal(0))
    result = sizer.size(_signal(), pf, get_settings())
    assert result.amount == Decimal(0)
    assert "zero_equity" in result.caps_applied


def test_zero_distance_unreachable_via_signal_construction() -> None:
    """The Signal __post_init__ already forbids zero-distance geometry.

    The protective ``zero_distance`` branch in :meth:`PositionSizer.size`
    is therefore unreachable through normal flow — it exists only as a
    belt-and-braces safety net should the dataclass invariants ever
    weaken. Documented here so the branch isn't mistaken for dead code.
    """
    with pytest.raises(ValueError):
        Signal(
            ticker="BTC/USDT",
            side="long",
            strength=0.7,
            timeframe="1h",
            entry_zone=(100.0, 100.0),
            invalidation=100.0,  # equal to entry → forbidden by post_init
            target_1=103.0,
            target_2=109.0,
            rationale="malformed",
            indicators={},
            generated_at=datetime(2026, 4, 27, tzinfo=UTC),
            strategy_id="scanner.oversold.v1",
            confidence_ai=None,
        )


def test_existing_ticker_exposure_reduces_headroom() -> None:
    """If existing exposure is at 80% of per-ticker cap, sizer caps
    further. With 10k equity, per-ticker cap 15% = 1500. If existing
    is 1200, headroom is 300. Natural size 1666.67 caps to 300.
    """
    sizer = PositionSizer()
    result = sizer.size(
        _signal(),
        _portfolio(),
        get_settings(),
        existing_ticker_exposure=Decimal("1200"),
    )
    assert "max_per_ticker" in result.caps_applied
    assert result.amount == Decimal("300")


def test_existing_exposure_at_cap_returns_zero() -> None:
    sizer = PositionSizer()
    result = sizer.size(
        _signal(),
        _portfolio(),
        get_settings(),
        existing_ticker_exposure=Decimal("1500"),  # exactly at 15% cap
    )
    assert result.amount == Decimal(0)
    assert "max_per_ticker" in result.caps_applied


def test_decimal_arithmetic_no_float_rounding() -> None:
    """Result is Decimal with at most 8 decimal places (exchange precision).
    Float intermediate noise (e.g. 0.1 + 0.2 != 0.3) cannot leak through.
    """
    sizer = PositionSizer()
    result = sizer.size(_signal(), _portfolio(), get_settings())
    # Quantization to 1e-8 → exponent <= -8.
    assert isinstance(result.amount, Decimal)
    # No noisy trailing digits beyond 8 decimal places.
    assert -result.amount.as_tuple().exponent <= 8
