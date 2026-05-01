"""Fill simulator Protocol + realistic + null implementations.

The :class:`FillSimulator` Protocol is the seam: production-equivalent
slippage modelling lives in :class:`SlippageFillSimulator` (FASE
12.2); :class:`NoFillSimulator` is a deterministic stand-in for
engine-only tests (FASE 12.1).

Slippage model (12.2):

- **Fixed bps**: every fill nudges the price by ``fixed_bps / 10000``
  in the unfavourable direction (buy fills above mid, sell fills
  below). Default 5 bps for crypto majors; equity has its own override.
- **Market impact**: ``impact_bps = market_impact_coefficient *
  notional / avg_volume_per_min``. Big orders relative to bar volume
  pay extra. Volume=0 bars set impact_bps=0 with a WARN log so the
  test feed doesn't silently inflate metrics.
- **Limit no-fill probability**: even when the limit price crosses,
  there's a 30% (default) chance the order doesn't get hit because
  liquidity moved away. Drawn from a seeded
  :class:`random.Random` so backtests are reproducible.
- **Stop slippage**: stops take ``fixed_bps × 1.5`` because triggered
  stops fire into stress (everyone selling at once).
- **Latency**: ``latency_ms_signal_to_order`` is informational; the
  engine already routes fills to ``next_bar.open`` so latency lower
  than the bar duration is absorbed implicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Literal, Protocol, runtime_checkable

from mib.backtest.types import BacktestBar

logger = logging.getLogger(__name__)

OrderType = Literal["market", "limit", "stop_market", "stop_limit"]
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class FillSimulationResult:
    """Outcome of one simulated order placement.

    ``filled`` is False when a limit order didn't cross or a partial-
    fill probability draw rejected the order. The engine then carries
    the order forward (limit) or treats it as failed (market would
    always fill in the simple model).
    """

    filled: bool
    fill_price: Decimal
    """Effective fill price after slippage. ``Decimal(0)`` when
    ``filled=False``."""

    filled_amount: Decimal
    """Amount actually filled, in base units. Equals the requested
    amount on a full fill, < requested on a partial."""

    fees_paid_quote: Decimal
    """Fee in the quote currency: ``filled_amount * fill_price *
    fee_pct``. ``Decimal(0)`` on no-fill."""

    fill_at: datetime
    """Timestamp the fill is recorded at (next bar's open by default
    so the engine ledger uses the right opened_at on entries)."""

    slippage_bps_applied: Decimal | None = None
    """Effective slippage in bps relative to the reference price.
    Useful for debugging extreme runs."""

    reason: str | None = None


@runtime_checkable
class FillSimulator(Protocol):
    """Contract every concrete fill simulator obeys."""

    def simulate_fill(
        self,
        *,
        side: OrderSide,
        order_type: OrderType,
        amount_base: Decimal,
        limit_price: Decimal | None,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,
    ) -> FillSimulationResult:
        """Synchronous, deterministic given the simulator's seed."""

    def reseed(self, seed: int) -> None:
        """Re-initialise RNG so a fresh run with the same seed
        produces byte-identical fills."""


# ─── Null implementation (12.1 testing aid) ─────────────────────────


class NoFillSimulator:
    """Always returns ``filled=True`` at next bar's open + zero fees.

    Used by 12.1 unit tests to exercise the engine without any
    slippage / partial-fill randomness. 12.2's
    :class:`SlippageFillSimulator` replaces this in production.
    """

    def __init__(self) -> None:
        self._seed: int = 0

    def simulate_fill(
        self,
        *,
        side: OrderSide,  # noqa: ARG002
        order_type: OrderType,  # noqa: ARG002
        amount_base: Decimal,
        limit_price: Decimal | None,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,  # noqa: ARG002
    ) -> FillSimulationResult:
        ref_bar = next_bar or current_bar
        fill_price = (
            limit_price
            if limit_price is not None
            else Decimal(str(ref_bar.candle.open))
        )
        return FillSimulationResult(
            filled=True,
            fill_price=fill_price,
            filled_amount=amount_base,
            fees_paid_quote=Decimal(0),
            fill_at=ref_bar.candle.timestamp,
        )

    def reseed(self, seed: int) -> None:
        self._seed = seed


# ─── SlippageFillSimulator (FASE 12.2) ──────────────────────────────


@dataclass(frozen=True)
class SlippageConfig:
    """Per-run slippage knobs.

    Defaults tuned for crypto majors (BTC/USDT, ETH/USDT). Equity
    backtests should override ``fixed_bps`` to ~2 bps. ``market_impact_
    coefficient`` is a linear approximation: real impact is sub-linear,
    but linear is fine for the regimes we backtest (small notional vs
    bar volume).
    """

    fixed_bps: Decimal = Decimal("5")
    """Always-applied slippage in basis points. Buy fills above mid,
    sell fills below."""

    market_impact_coefficient: Decimal = Decimal("0.1")
    """``impact_bps = coef × notional / avg_volume_per_min``."""

    limit_no_fill_probability: Decimal = Decimal("0.30")
    """Chance a limit order is rejected even when price crosses."""

    stop_extra_bps_multiplier: Decimal = Decimal("1.5")
    """Stops trigger into stress; multiply ``fixed_bps`` by this."""

    latency_ms_signal_to_order: int = 100
    """Informational. The bar-replay model absorbs latency below the
    bar duration; this is logged for completeness."""


_BPS_DENOM: Decimal = Decimal(10000)


class SlippageFillSimulator:
    """Production-shaped simulator: slippage + impact + RNG-driven no-fill.

    Deterministic by construction: same ``seed`` + same input feed
    produces byte-identical fill decisions. The seed is wired through
    :class:`mib.backtest.types.BacktestSettings.random_seed` so the
    operator can reproduce a run exactly from the persisted config.
    """

    def __init__(
        self,
        config: SlippageConfig | None = None,
        *,
        seed: int = 0,
    ) -> None:
        self._config: SlippageConfig = config or SlippageConfig()
        self._rng: Random = Random(seed)
        self._seed: int = seed

    def reseed(self, seed: int) -> None:
        self._rng = Random(seed)
        self._seed = seed

    def simulate_fill(
        self,
        *,
        side: OrderSide,
        order_type: OrderType,
        amount_base: Decimal,
        limit_price: Decimal | None,
        current_bar: BacktestBar,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,
    ) -> FillSimulationResult:
        ref_bar = next_bar or current_bar
        ref_ts = ref_bar.candle.timestamp
        if order_type == "market":
            return self._simulate_market(
                side=side,
                amount_base=amount_base,
                ref_bar=ref_bar,
                fee_pct=fee_pct,
                fill_at=ref_ts,
            )
        if order_type == "limit":
            return self._simulate_limit(
                side=side,
                amount_base=amount_base,
                limit_price=limit_price,
                next_bar=next_bar,
                fee_pct=fee_pct,
                fill_at=ref_ts,
            )
        if order_type in ("stop_market", "stop_limit"):
            return self._simulate_stop(
                side=side,
                amount_base=amount_base,
                stop_price=limit_price,
                next_bar=next_bar,
                fee_pct=fee_pct,
                fill_at=ref_ts,
            )
        return _no_fill(reason=f"unknown_order_type:{order_type}", at=ref_ts)

    # ─── Per-type implementations ─────────────────────────────────

    def _simulate_market(
        self,
        *,
        side: OrderSide,
        amount_base: Decimal,
        ref_bar: BacktestBar,
        fee_pct: Decimal,
        fill_at: datetime,
    ) -> FillSimulationResult:
        mid = Decimal(str(ref_bar.candle.open))
        if mid <= 0:
            return _no_fill(reason="reference_price_non_positive", at=fill_at)
        notional = mid * amount_base
        impact_bps = self._market_impact_bps(
            notional=notional, ref_bar=ref_bar
        )
        total_bps = self._config.fixed_bps + impact_bps
        fill_price = _apply_slippage(mid=mid, side=side, bps=total_bps)
        fees = (fill_price * amount_base * fee_pct).quantize(
            Decimal("0.00000001")
        )
        return FillSimulationResult(
            filled=True,
            fill_price=fill_price.quantize(Decimal("0.00000001")),
            filled_amount=amount_base,
            fees_paid_quote=fees,
            fill_at=fill_at,
            slippage_bps_applied=total_bps,
        )

    def _simulate_limit(
        self,
        *,
        side: OrderSide,
        amount_base: Decimal,
        limit_price: Decimal | None,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,
        fill_at: datetime,
    ) -> FillSimulationResult:
        if limit_price is None or limit_price <= 0:
            return _no_fill(reason="limit_price_missing", at=fill_at)
        if next_bar is None:
            return _no_fill(reason="no_next_bar_for_limit", at=fill_at)
        next_high = Decimal(str(next_bar.candle.high))
        next_low = Decimal(str(next_bar.candle.low))

        # Crossing check: a buy limit is hit when next bar's low <= limit;
        # a sell limit is hit when next bar's high >= limit.
        crossed = (
            limit_price >= next_low if side == "buy" else limit_price <= next_high
        )
        if not crossed:
            return _no_fill(reason="limit_did_not_cross", at=fill_at)

        # Probabilistic no-fill — even when crossed, liquidity may have
        # moved before our order reached the book. Seeded RNG keeps it
        # reproducible.
        roll = Decimal(str(self._rng.random()))
        if roll < self._config.limit_no_fill_probability:
            return _no_fill(
                reason="limit_no_fill_random",
                at=fill_at,
            )

        # Filled at the limit price exactly (limit gives price, not
        # better) — no slippage on a successful limit beyond the queue
        # rejection probability above.
        fees = (limit_price * amount_base * fee_pct).quantize(
            Decimal("0.00000001")
        )
        return FillSimulationResult(
            filled=True,
            fill_price=limit_price.quantize(Decimal("0.00000001")),
            filled_amount=amount_base,
            fees_paid_quote=fees,
            fill_at=fill_at,
            slippage_bps_applied=Decimal(0),
        )

    def _simulate_stop(
        self,
        *,
        side: OrderSide,
        amount_base: Decimal,
        stop_price: Decimal | None,
        next_bar: BacktestBar | None,
        fee_pct: Decimal,
        fill_at: datetime,
    ) -> FillSimulationResult:
        if stop_price is None or stop_price <= 0:
            return _no_fill(reason="stop_price_missing", at=fill_at)
        if next_bar is None:
            return _no_fill(reason="no_next_bar_for_stop", at=fill_at)
        next_high = Decimal(str(next_bar.candle.high))
        next_low = Decimal(str(next_bar.candle.low))

        # Stop semantics flipped from limit: sell-stop (long exit) fires
        # when next.low <= stop; buy-stop (short exit) fires when
        # next.high >= stop.
        triggered = (
            next_low <= stop_price if side == "sell" else next_high >= stop_price
        )
        if not triggered:
            return _no_fill(reason="stop_not_hit", at=fill_at)

        # Stops fill into stress: extra slippage beyond fixed_bps.
        stop_bps = (
            self._config.fixed_bps * self._config.stop_extra_bps_multiplier
        )
        fill_price = _apply_slippage(mid=stop_price, side=side, bps=stop_bps)
        fees = (fill_price * amount_base * fee_pct).quantize(
            Decimal("0.00000001")
        )
        return FillSimulationResult(
            filled=True,
            fill_price=fill_price.quantize(Decimal("0.00000001")),
            filled_amount=amount_base,
            fees_paid_quote=fees,
            fill_at=fill_at,
            slippage_bps_applied=stop_bps,
        )

    def _market_impact_bps(
        self, *, notional: Decimal, ref_bar: BacktestBar
    ) -> Decimal:
        """``impact_bps = coef * notional / avg_volume_per_min``.

        ``avg_volume_per_min`` is derived from the bar's volume + the
        bar duration. We need the duration; absent an explicit field we
        treat the bar as 1h (3600s = 60 min). That's the common
        production timeframe; smaller intervals will under-impact and
        larger ones will over-impact, but the operator can tune via
        ``market_impact_coefficient``.
        """
        volume = Decimal(str(ref_bar.candle.volume))
        if volume <= 0:
            logger.warning(
                "backtest: volume=0 on bar %s; impact_bps forced to 0",
                ref_bar.candle.timestamp.isoformat(),
            )
            return Decimal(0)
        bar_minutes = _bar_duration_minutes(ref_bar)
        if bar_minutes <= 0:
            return Decimal(0)
        avg_vol_per_min = volume / Decimal(bar_minutes)
        if avg_vol_per_min <= 0:
            return Decimal(0)
        return (
            self._config.market_impact_coefficient * notional / avg_vol_per_min
        ).quantize(Decimal("0.00000001"))


# ─── Pure helpers ───────────────────────────────────────────────────


def _apply_slippage(*, mid: Decimal, side: OrderSide, bps: Decimal) -> Decimal:
    """Return the effective fill price after applying ``bps`` of slippage.

    Buy fills slip ABOVE the reference price (you pay more); sell fills
    slip BELOW it (you receive less).
    """
    factor = Decimal(1) + bps / _BPS_DENOM
    if side == "buy":
        return mid * factor
    return mid / factor


def _bar_duration_minutes(ref_bar: BacktestBar) -> int:
    """Best-effort bar duration. Defaults to 60 (1h timeframe).

    A future enhancement: thread the actual timeframe through
    :class:`BacktestBar` so this is exact. For now 60 is the default
    used by the production strategy presets.
    """
    _ = ref_bar  # placeholder for future timeframe inspection
    return 60


def _no_fill(*, reason: str, at: datetime) -> FillSimulationResult:
    return FillSimulationResult(
        filled=False,
        fill_price=Decimal(0),
        filled_amount=Decimal(0),
        fees_paid_quote=Decimal(0),
        fill_at=at,
        slippage_bps_applied=None,
        reason=reason,
    )


# Suppress unused-import: kept for future fine-grained latency modelling.
_ = timedelta
