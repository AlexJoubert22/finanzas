"""Frozen :class:`RiskDecision` value object.

Every call to :meth:`mib.trading.risk.manager.RiskManager.evaluate`
produces one. The repository in :mod:`mib.trading.risk.repo` persists
it append-only: re-evaluating the same signal yields a NEW row with
``version = previous + 1``. Updates to existing decisions are
forbidden by the repository contract and by the DB unique constraint.

``sized_amount`` is None until FASE 8.5 wires the position sizer; the
field exists from FASE 8.3 so the schema doesn't change between
sub-commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from mib.trading.risk.protocol import GateResult


@dataclass(frozen=True)
class RiskDecision:
    """Immutable record of one risk evaluation."""

    signal_id: int
    version: int
    approved: bool
    gate_results: tuple[GateResult, ...]
    reasoning: str
    decided_at: datetime
    sized_amount: Decimal | None = None

    def __post_init__(self) -> None:
        if self.signal_id < 1:
            raise ValueError(
                f"signal_id must be >= 1 (got {self.signal_id})"
            )
        if self.version < 1:
            raise ValueError(f"version must be >= 1 (got {self.version})")
        if self.sized_amount is not None and self.sized_amount < 0:
            raise ValueError(
                f"sized_amount must be >= 0 when set (got {self.sized_amount})"
            )
