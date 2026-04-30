"""Risk-based position sizer.

Formula
-------

::

    risk_per_trade = equity * risk_per_trade_pct
    distance_to_stop = |entry - invalidation|
    size_units      = risk_per_trade / distance_to_stop
    size_quote      = size_units * entry

After computing the natural size, four caps are applied in order:

1. ``max_per_ticker`` — capped at ``max_exposure_per_ticker_pct *
   equity`` minus the existing ticker exposure.
2. ``max_position_pct`` — capped at ``max_position_pct * equity``.
3. ``available_cash`` — capped at the free balance in the quote
   currency.
4. ``min_notional`` — if the resulting size is below
   ``min_notional_quote``, return 0 with an explicit reason.

The order matters: a lower cap further down won't be undone by a
higher cap upstream. Caps fired during sizing are logged in
:class:`SizerResult.caps_applied` so the operator can see which
constraint dominated.

All money math uses :class:`decimal.Decimal`. Float is forbidden in
this module per the master prompt's risk-layer rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from mib.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


@dataclass(frozen=True)
class SizerResult:
    """Outcome of one sizing call.

    ``amount`` is in quote currency (EUR by default). When the
    chained caps push the result under ``min_notional``, ``amount``
    is :class:`Decimal('0')` and ``caps_applied`` includes
    ``"min_notional"``.
    """

    amount: Decimal
    reasoning: str
    caps_applied: tuple[str, ...]


class PositionSizer:
    """Compute position size in quote currency from a Signal."""

    def __init__(self) -> None:
        # Stateless — kept as a class for future extension (per-strategy
        # sizing modifiers, AI confidence scaling in FASE 11, etc.).
        pass

    def size(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        settings: Settings,
        *,
        existing_ticker_exposure: Decimal = Decimal(0),
    ) -> SizerResult:
        equity = portfolio.equity_quote
        if equity <= 0:
            return SizerResult(
                amount=Decimal(0),
                reasoning="equity is 0 or negative; no sizing possible",
                caps_applied=("zero_equity",),
            )

        # Distance to stop is computed from the entry midpoint.
        low, high = signal.entry_zone
        entry_mid = (Decimal(str(low)) + Decimal(str(high))) / Decimal(2)
        invalidation = Decimal(str(signal.invalidation))
        distance = abs(entry_mid - invalidation)
        if distance <= 0:
            return SizerResult(
                amount=Decimal(0),
                reasoning=(
                    "distance_to_stop is 0 — entry_zone collapses onto "
                    "invalidation; signal is malformed"
                ),
                caps_applied=("zero_distance",),
            )

        risk_pct = Decimal(str(settings.risk_per_trade_pct))
        risk_quote = risk_pct * equity
        size_units = risk_quote / distance
        size_quote = size_units * entry_mid

        caps: list[str] = []

        # Cap 1: per-ticker exposure cap minus existing exposure.
        per_ticker_cap = Decimal(str(settings.max_exposure_per_ticker_pct)) * equity
        per_ticker_headroom = per_ticker_cap - existing_ticker_exposure
        if per_ticker_headroom <= 0:
            return SizerResult(
                amount=Decimal(0),
                reasoning=(
                    f"per-ticker headroom exhausted: existing exposure "
                    f"{existing_ticker_exposure} >= cap {per_ticker_cap}"
                ),
                caps_applied=("max_per_ticker",),
            )
        if size_quote > per_ticker_headroom:
            size_quote = per_ticker_headroom
            caps.append("max_per_ticker")

        # Cap 2: max single-position fraction of equity.
        max_pos = Decimal(str(settings.max_position_pct)) * equity
        if size_quote > max_pos:
            size_quote = max_pos
            caps.append("max_position_pct")

        # Cap 3: available cash in quote currency.
        available = _available_cash(portfolio)
        if size_quote > available:
            size_quote = available
            caps.append("available_cash")

        # Cap 4: min notional threshold. If we land below it, return 0
        # with an explicit reason. The signal isn't tradable today.
        min_notional = Decimal(str(settings.min_notional_quote))
        if size_quote < min_notional:
            return SizerResult(
                amount=Decimal(0),
                reasoning=(
                    f"size {size_quote} < min_notional {min_notional} "
                    f"after caps {caps}; signal is below tradable threshold"
                ),
                caps_applied=(*caps, "min_notional"),
            )

        # Round down to 8 decimals (typical exchange precision) to
        # avoid float-style noise in the final value.
        size_quote = size_quote.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

        reasoning = (
            f"sized at {size_quote} {settings.timezone[:3] if False else 'EUR'} "
            f"(risk {risk_pct:.3%} × equity {equity} = {risk_quote}; "
            f"caps applied: {caps if caps else 'none'})"
        )
        logger.debug(
            "position_sizer: ticker={} size_quote={} caps={} risk_quote={} "
            "distance={}",
            signal.ticker,
            size_quote,
            caps,
            risk_quote,
            distance,
        )
        return SizerResult(
            amount=size_quote, reasoning=reasoning, caps_applied=tuple(caps)
        )


def _available_cash(portfolio: PortfolioSnapshot) -> Decimal:
    """Sum of free balances in the quote currency (EUR by default).

    For now we treat every balance whose asset matches "EUR" as
    quote-side cash. Multi-quote portfolios (e.g. EUR + USDT) need a
    valuation step that lives in FASE 9.
    """
    total = Decimal(0)
    for b in portfolio.balances:
        if b.asset.upper() == "EUR":
            total += b.free
    return total
