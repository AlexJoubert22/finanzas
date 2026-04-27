"""Signal dataclass and the small set of helpers that derive its
risk/target levels.

Two responsibilities, kept distinct on purpose:

1. ``derive_invalidation_long`` / ``derive_invalidation_short`` — convert
   an entry price plus a volatility measure (ATR) into the price level
   where the trade thesis is dead. This is the *only* place ATR enters
   the trading layer.
2. ``derive_targets`` — convert an entry plus the invalidation distance
   into target prices expressed in **R-multiples** (units of risk
   assumed). This deliberately does not take ATR: targets are universal
   units (1R = the same risk taken across strategies), so expectancy
   numbers in FASE 12 backtests are comparable across strategies that
   chose different ``k_invalidation`` values.

The :class:`Signal` itself is a frozen dataclass with a strong
``__post_init__`` invariant: any signal that survives construction has
an internally consistent (entry, invalidation, target) geometry on the
side it claims to take. Bad upstream callers fail loud at construction
time, not three layers deeper inside the executor.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Side = Literal["long", "short", "flat"]

# Strategy ids must be namespaced + versioned so historical signals
# remain traceable to the algorithm that produced them. Examples that
# pass: ``scanner.oversold.v1``, ``ai.macro_breakout.v3``. Examples that
# fail: ``oversold``, ``scanner.oversold``, ``scanner_oversold_v1``.
_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+\.v\d+$")


@dataclass(frozen=True)
class Signal:
    """A fully-specified trade thesis. Construction enforces consistency.

    A ``Signal`` is the only object the rest of the trading layer
    accepts. Anything that cannot fill every required field — most
    commonly because :class:`mib.models.market.TechnicalSnapshot` had
    no ``atr_14`` — must NOT emit a signal.

    The ``entry_zone`` is a closed interval ``[low, high]``. For limit
    orders the executor places at ``low`` (long) or ``high`` (short);
    for market orders it ignores the zone and uses the live quote.
    """

    ticker: str
    side: Side
    strength: float                       # 0.0 .. 1.0
    timeframe: str                        # "1h", "4h", "1d", …
    entry_zone: tuple[float, float]       # (low, high), low <= high
    invalidation: float                   # stop level (price where thesis dies)
    target_1: float                       # 1R by default (see derive_targets)
    target_2: float | None                # 3R by default; None if not used
    rationale: str                        # plain-text justification, for logs/UI
    indicators: dict[str, float] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    strategy_id: str = ""                 # namespaced + versioned
    confidence_ai: float | None = None    # 0..1, None if AI not consulted

    def __post_init__(self) -> None:
        self._check_basic_shape()
        if self.side != "flat":
            self._check_directional_geometry()

    # ─── Validators (private, called from __post_init__) ───────────

    def _check_basic_shape(self) -> None:
        if not self.ticker.strip():
            raise ValueError("Signal.ticker must not be empty")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(
                f"Signal.strength must be in [0, 1] (got {self.strength})"
            )
        low, high = self.entry_zone
        if not (low > 0.0 and high > 0.0):
            raise ValueError(
                f"Signal.entry_zone prices must be positive (got {self.entry_zone})"
            )
        if low > high:
            raise ValueError(
                f"Signal.entry_zone is (low, high); got low={low} > high={high}"
            )
        if self.invalidation <= 0.0:
            raise ValueError(
                f"Signal.invalidation must be a positive price (got {self.invalidation})"
            )
        if self.target_1 <= 0.0:
            raise ValueError(
                f"Signal.target_1 must be positive (got {self.target_1})"
            )
        if self.target_2 is not None and self.target_2 <= 0.0:
            raise ValueError(
                f"Signal.target_2 must be positive when set (got {self.target_2})"
            )
        if self.confidence_ai is not None and not 0.0 <= self.confidence_ai <= 1.0:
            raise ValueError(
                f"Signal.confidence_ai must be in [0, 1] (got {self.confidence_ai})"
            )
        if not _STRATEGY_ID_RE.match(self.strategy_id):
            raise ValueError(
                "Signal.strategy_id must be namespaced and versioned, "
                f"e.g. 'scanner.oversold.v1' (got {self.strategy_id!r})"
            )

    def _check_directional_geometry(self) -> None:
        """For long/short, verify stop and targets sit on the right sides.

        - Long: invalidation must be **below** the entry zone, every
          target must be **above** it; ``target_2 > target_1`` if both
          are set.
        - Short: mirrored.
        """
        low, high = self.entry_zone
        if self.side == "long":
            if self.invalidation >= low:
                raise ValueError(
                    f"long: invalidation {self.invalidation} must be < "
                    f"entry_zone low {low}"
                )
            if self.target_1 <= high:
                raise ValueError(
                    f"long: target_1 {self.target_1} must be > "
                    f"entry_zone high {high}"
                )
            if self.target_2 is not None and self.target_2 <= self.target_1:
                raise ValueError(
                    f"long: target_2 {self.target_2} must be > "
                    f"target_1 {self.target_1}"
                )
        else:  # "short"
            if self.invalidation <= high:
                raise ValueError(
                    f"short: invalidation {self.invalidation} must be > "
                    f"entry_zone high {high}"
                )
            if self.target_1 >= low:
                raise ValueError(
                    f"short: target_1 {self.target_1} must be < "
                    f"entry_zone low {low}"
                )
            if self.target_2 is not None and self.target_2 >= self.target_1:
                raise ValueError(
                    f"short: target_2 {self.target_2} must be < "
                    f"target_1 {self.target_1}"
                )


# ─── Derivation helpers ─────────────────────────────────────────────

def derive_invalidation_long(entry: float, atr: float, k: float = 1.5) -> float:
    """Return ``entry - k*atr`` after rejecting nonsense inputs."""
    _check_atr_inputs(entry=entry, atr=atr, k=k)
    return entry - k * atr


def derive_invalidation_short(entry: float, atr: float, k: float = 1.5) -> float:
    """Return ``entry + k*atr`` after rejecting nonsense inputs."""
    _check_atr_inputs(entry=entry, atr=atr, k=k)
    return entry + k * atr


def derive_targets(
    entry: float,
    invalidation: float,
    *,
    side: Literal["long", "short"],
    r_multiples: Sequence[float] = (1.0, 3.0),
) -> tuple[float, float | None]:
    """Translate an (entry, stop) pair into target prices in R-multiples.

    ``r_multiples`` is a sequence of multiples of the risk distance
    ``|entry - invalidation|``. The default ``(1.0, 3.0)`` is the
    classic scale-out: half off at 1R (covers fees + lets you free-roll
    by moving stop to breakeven), half left to run to 3R.

    Returns ``(target_1, target_2)`` where ``target_2 is None`` if the
    sequence has fewer than two elements. Anything beyond two values is
    silently dropped — the :class:`Signal` schema only models two
    targets today; extending it to N is a FASE 8 concern.
    """
    if entry <= 0.0:
        raise ValueError(f"entry must be a positive price (got {entry})")
    if invalidation <= 0.0:
        raise ValueError(f"invalidation must be a positive price (got {invalidation})")
    if entry == invalidation:
        raise ValueError("entry == invalidation: zero-risk position is undefined")
    if not r_multiples:
        raise ValueError("r_multiples must contain at least one ratio")
    if any(r <= 0.0 for r in r_multiples):
        raise ValueError(f"r_multiples must all be > 0 (got {r_multiples!r})")

    if side == "long":
        if invalidation >= entry:
            raise ValueError(
                f"long: invalidation {invalidation} must be below entry {entry}"
            )
        risk = entry - invalidation
        targets = [entry + r * risk for r in r_multiples]
    else:  # "short"
        if invalidation <= entry:
            raise ValueError(
                f"short: invalidation {invalidation} must be above entry {entry}"
            )
        risk = invalidation - entry
        targets = [entry - r * risk for r in r_multiples]

    t1 = targets[0]
    t2 = targets[1] if len(targets) >= 2 else None
    return t1, t2


def _check_atr_inputs(*, entry: float, atr: float, k: float) -> None:
    if entry <= 0.0:
        raise ValueError(f"entry must be a positive price (got {entry})")
    if atr <= 0.0:
        raise ValueError(f"atr must be > 0 (got {atr})")
    if k <= 0.0:
        raise ValueError(f"k must be > 0 (got {k})")
