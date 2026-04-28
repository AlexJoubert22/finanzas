"""Unit tests for the Signal dataclass and the ATR/R derivation helpers.

These are pure-logic tests with no DB and no network — they protect
the geometric invariants the rest of FASE 7 will rely on.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from mib.trading.signals import (
    Signal,
    derive_invalidation_long,
    derive_invalidation_short,
    derive_targets,
)

# ─── Derivation helpers ────────────────────────────────────────────────

class TestDeriveInvalidation:
    def test_long_default_k(self) -> None:
        assert derive_invalidation_long(entry=100.0, atr=2.0) == pytest.approx(97.0)

    def test_short_default_k(self) -> None:
        assert derive_invalidation_short(entry=100.0, atr=2.0) == pytest.approx(103.0)

    def test_long_custom_k(self) -> None:
        assert derive_invalidation_long(100.0, atr=2.0, k=2.5) == pytest.approx(95.0)

    @pytest.mark.parametrize(
        "entry, atr, k",
        [
            (-1.0, 2.0, 1.5),  # negative entry
            (0.0, 2.0, 1.5),   # zero entry
            (100.0, 0.0, 1.5),  # zero atr
            (100.0, -2.0, 1.5),  # negative atr
            (100.0, 2.0, 0.0),  # zero k
            (100.0, 2.0, -1.0),  # negative k
        ],
    )
    def test_rejects_garbage_inputs(self, entry: float, atr: float, k: float) -> None:
        with pytest.raises(ValueError):
            derive_invalidation_long(entry=entry, atr=atr, k=k)


class TestDeriveTargets:
    def test_long_default_r_multiples(self) -> None:
        # entry=100, stop=98 → risk=2 → t1=102 (1R), t2=106 (3R).
        t1, t2 = derive_targets(100.0, 98.0, side="long")
        assert t1 == pytest.approx(102.0)
        assert t2 == pytest.approx(106.0)

    def test_short_default_r_multiples(self) -> None:
        # entry=100, stop=102 → risk=2 → t1=98 (1R), t2=94 (3R).
        t1, t2 = derive_targets(100.0, 102.0, side="short")
        assert t1 == pytest.approx(98.0)
        assert t2 == pytest.approx(94.0)

    def test_single_r_multiple_returns_none_for_target_2(self) -> None:
        t1, t2 = derive_targets(100.0, 98.0, side="long", r_multiples=(2.0,))
        assert t1 == pytest.approx(104.0)
        assert t2 is None

    def test_extra_r_multiples_are_ignored(self) -> None:
        t1, t2 = derive_targets(
            100.0, 98.0, side="long", r_multiples=(1.0, 3.0, 5.0, 8.0)
        )
        assert (t1, t2) == pytest.approx((102.0, 106.0))

    def test_long_rejects_invalidation_above_entry(self) -> None:
        with pytest.raises(ValueError, match="invalidation"):
            derive_targets(100.0, 105.0, side="long")

    def test_short_rejects_invalidation_below_entry(self) -> None:
        with pytest.raises(ValueError, match="invalidation"):
            derive_targets(100.0, 95.0, side="short")

    def test_zero_risk_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="zero-risk"):
            derive_targets(100.0, 100.0, side="long")

    def test_empty_r_multiples_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            derive_targets(100.0, 98.0, side="long", r_multiples=())

    def test_non_positive_r_multiples_rejected(self) -> None:
        with pytest.raises(ValueError, match="r_multiples"):
            derive_targets(100.0, 98.0, side="long", r_multiples=(1.0, 0.0, 3.0))


# ─── Signal construction — happy paths ────────────────────────────────

def _valid_long_signal(**overrides: object) -> Signal:
    """Build a syntactically-valid long signal; tests override one field."""
    base: dict[str, object] = {
        "ticker": "BTC/USDT",
        "side": "long",
        "strength": 0.7,
        "timeframe": "1h",
        "entry_zone": (100.0, 101.0),
        "invalidation": 97.0,
        "target_1": 103.0,
        "target_2": 109.0,
        "rationale": "RSI<30 + volume spike",
        "indicators": {"rsi_14": 22.5, "atr_14": 2.0},
        "generated_at": datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        "strategy_id": "scanner.oversold.v1",
        "confidence_ai": None,
    }
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


class TestSignalHappyPath:
    def test_minimal_long_signal_constructs(self) -> None:
        s = _valid_long_signal()
        assert s.side == "long"
        assert s.strategy_id == "scanner.oversold.v1"

    def test_short_signal_with_mirror_geometry(self) -> None:
        s = _valid_long_signal(
            side="short",
            entry_zone=(100.0, 101.0),
            invalidation=103.0,
            target_1=99.0,
            target_2=95.0,
            strategy_id="scanner.breakdown.v1",
        )
        assert s.side == "short"

    def test_target_2_optional(self) -> None:
        s = _valid_long_signal(target_2=None)
        assert s.target_2 is None

    def test_flat_signal_skips_directional_checks(self) -> None:
        # Flat signals are exit hints; geometry of stops/targets is
        # not enforced because the engine isn't entering anything.
        s = _valid_long_signal(side="flat", invalidation=99.0, target_1=99.5)
        assert s.side == "flat"

    def test_constructed_signal_is_immutable(self) -> None:
        s = _valid_long_signal()
        with pytest.raises(FrozenInstanceError):
            s.ticker = "ETH/USDT"  # type: ignore[misc]


# ─── Signal construction — error paths ────────────────────────────────

class TestSignalValidation:
    def test_empty_ticker(self) -> None:
        with pytest.raises(ValueError, match="ticker"):
            _valid_long_signal(ticker="")

    @pytest.mark.parametrize("strength", [-0.1, 1.5, 2.0])
    def test_strength_out_of_range(self, strength: float) -> None:
        with pytest.raises(ValueError, match="strength"):
            _valid_long_signal(strength=strength)

    def test_entry_zone_inverted(self) -> None:
        with pytest.raises(ValueError, match="entry_zone"):
            _valid_long_signal(entry_zone=(101.0, 100.0))

    def test_long_invalidation_above_entry_zone(self) -> None:
        with pytest.raises(ValueError, match="invalidation"):
            _valid_long_signal(invalidation=100.5)

    def test_long_target_below_entry_zone(self) -> None:
        with pytest.raises(ValueError, match="target_1"):
            _valid_long_signal(target_1=100.5)

    def test_long_target_2_below_target_1(self) -> None:
        with pytest.raises(ValueError, match="target_2"):
            _valid_long_signal(target_1=105.0, target_2=104.0)

    def test_short_invalidation_below_entry_zone(self) -> None:
        with pytest.raises(ValueError, match="invalidation"):
            _valid_long_signal(
                side="short",
                entry_zone=(100.0, 101.0),
                invalidation=100.5,  # must be > 101
                target_1=99.0,
                target_2=95.0,
                strategy_id="scanner.breakdown.v1",
            )

    @pytest.mark.parametrize(
        "bad_id",
        [
            "oversold",                   # no namespace
            "scanner.oversold",           # missing version suffix
            "scanner_oversold_v1",        # underscores instead of dots
            "Scanner.Oversold.v1",        # capitals not allowed
            "scanner.oversold.v",         # version digits missing
            "scanner.oversold.1",         # 'v' missing
            "",                            # empty
        ],
    )
    def test_strategy_id_must_be_namespaced_and_versioned(self, bad_id: str) -> None:
        with pytest.raises(ValueError, match="strategy_id"):
            _valid_long_signal(strategy_id=bad_id)

    @pytest.mark.parametrize(
        "good_id",
        [
            "scanner.oversold.v1",
            "scanner.breakout.v12",
            "ai.macro_breakout.v3",
            "ai.news_reactor.bearish.v2",
        ],
    )
    def test_strategy_id_accepts_well_formed(self, good_id: str) -> None:
        s = _valid_long_signal(strategy_id=good_id)
        assert s.strategy_id == good_id

    @pytest.mark.parametrize("conf", [-0.1, 1.5])
    def test_confidence_ai_out_of_range(self, conf: float) -> None:
        with pytest.raises(ValueError, match="confidence_ai"):
            _valid_long_signal(confidence_ai=conf)


# ─── Roundtrip — helpers feed Signal cleanly ─────────────────────────

class TestHelpersFeedSignal:
    def test_long_helpers_produce_valid_signal(self) -> None:
        entry = 60_000.0
        atr = 800.0
        invalidation = derive_invalidation_long(entry, atr, k=1.5)
        t1, t2 = derive_targets(entry, invalidation, side="long")
        # entry_zone bracketing the entry (a single price collapses to a
        # one-tick zone — choose a tiny positive width for realism).
        s = Signal(
            ticker="BTC/USDT",
            side="long",
            strength=0.6,
            timeframe="1h",
            entry_zone=(entry, entry + 1.0),
            invalidation=invalidation,
            target_1=t1,
            target_2=t2,
            rationale="ATR-derived stop test",
            indicators={"atr_14": atr},
            strategy_id="scanner.oversold.v1",
        )
        # 1R = 1 * (entry - invalidation) = 1.5 * atr = 1200 → t1 = 61200
        assert s.target_1 == pytest.approx(entry + 1.5 * atr)
        # 3R = 3 * 1.5 * atr = 3600 → t2 = 63600
        assert s.target_2 == pytest.approx(entry + 3 * 1.5 * atr)

    def test_short_helpers_produce_valid_signal(self) -> None:
        entry = 60_000.0
        atr = 800.0
        invalidation = derive_invalidation_short(entry, atr, k=1.5)
        t1, t2 = derive_targets(entry, invalidation, side="short")
        s = Signal(
            ticker="BTC/USDT",
            side="short",
            strength=0.6,
            timeframe="1h",
            entry_zone=(entry - 1.0, entry),
            invalidation=invalidation,
            target_1=t1,
            target_2=t2,
            rationale="mirror of the long path",
            indicators={"atr_14": atr},
            strategy_id="scanner.breakdown.v1",
        )
        assert s.target_1 == pytest.approx(entry - 1.5 * atr)
        assert s.target_2 == pytest.approx(entry - 3 * 1.5 * atr)
