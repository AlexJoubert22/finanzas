"""Gate :class:`~typing.Protocol` and :class:`GateResult` dataclass.

A *gate* is a small async predicate over ``(signal, portfolio,
settings)`` that returns a :class:`GateResult` describing whether the
trade may proceed and why. Concrete gates live under
``mib.trading.risk.gates`` and are registered (in priority order) on
:class:`RiskManager` at construction time.

The protocol is :class:`typing.Protocol` (not ``abc.ABC``) on purpose:
duck-typed implementations are easier to mock in tests and any future
external module can satisfy the contract without subclassing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from mib.config import Settings
    from mib.models.portfolio import PortfolioSnapshot
    from mib.trading.signals import Signal


@dataclass(frozen=True)
class GateResult:
    """Outcome of a single gate check."""

    passed: bool
    reason: str
    gate_name: str


@runtime_checkable
class Gate(Protocol):
    """Common shape for every risk gate.

    ``name`` is a stable string id used in :class:`GateResult` and in
    log/metric labels. Concrete gates expose it as a ``ClassVar[str]``
    so it's accessible without instantiation.
    """

    name: str

    async def check(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        settings: Settings,
    ) -> GateResult: ...
