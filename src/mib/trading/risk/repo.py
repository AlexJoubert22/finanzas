"""Append-only repository for :class:`RiskDecision`.

Per ROADMAP.md Parte 0 mandate: ``add()`` is INSERT-only. Re-
evaluating the same signal produces a new row with the next version.
The DB-level UNIQUE constraint on ``(signal_id, version)`` guarantees
two concurrent appends with the same version cannot both succeed —
the loser sees an :class:`IntegrityError` which the repo translates
to :class:`RiskDecisionVersionMismatchError`.

The convenience helper :meth:`append_with_retry` wraps the
"compute next version → build decision → add → retry on conflict"
pattern callers in FASE 8.6 will use.

Mypy strict is intentionally NOT applied here (SQLAlchemy
``Mapped[T]`` inference fights strict). The strict surface is the
pure-logic siblings: :mod:`protocol`, :mod:`decision`, :mod:`manager`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import RiskDecisionRow
from mib.logger import logger
from mib.trading.risk.decision import RiskDecision
from mib.trading.risk.protocol import GateResult


class RiskDecisionVersionMismatchError(Exception):
    """Raised when ``add()`` receives a decision whose ``version`` does
    not equal ``existing_count + 1`` for that ``signal_id``.

    Indicates either a programming bug (caller computed version wrong)
    or a race (another evaluator inserted between read and write).
    Callers can recover via :meth:`RiskDecisionRepository.append_with_retry`
    which recomputes and retries.
    """

    def __init__(self, signal_id: int, expected: int, actual: int) -> None:
        super().__init__(
            f"signal #{signal_id}: expected version={expected}, got {actual}"
        )
        self.signal_id = signal_id
        self.expected = expected
        self.actual = actual


class RiskDecisionRepository:
    """CRUD-but-append-only for ``risk_decisions``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add(self, decision: RiskDecision) -> int:
        """Insert ``decision`` if its version matches ``next_expected``.

        Returns the inserted row id. Raises
        :class:`RiskDecisionVersionMismatchError` on mismatch.
        """
        async with self._sf() as session:
            async with session.begin():
                expected = await self._next_version_in_session(
                    session, decision.signal_id
                )
                if decision.version != expected:
                    raise RiskDecisionVersionMismatchError(
                        decision.signal_id, expected, decision.version
                    )
                row = _to_row(decision)
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    # Race: another transaction won the same version slot
                    # between our count and our flush.
                    raise RiskDecisionVersionMismatchError(
                        decision.signal_id, expected, decision.version
                    ) from exc
                row_id: int = row.id
            logger.debug(
                "risk_decision_repo: added id={} signal_id={} version={} approved={}",
                row_id,
                decision.signal_id,
                decision.version,
                decision.approved,
            )
            return row_id

    async def append_with_retry(
        self,
        signal_id: int,
        decision_factory: Callable[[int], RiskDecision],
        *,
        max_retries: int = 3,
    ) -> RiskDecision:
        """Read next version, build decision via factory, add. Retry on race.

        ``decision_factory(version)`` must produce a :class:`RiskDecision`
        with the supplied version. The helper catches
        :class:`RiskDecisionVersionMismatchError` (race with a parallel
        evaluator) and retries up to ``max_retries`` times.
        """
        last_err: RiskDecisionVersionMismatchError | None = None
        for _ in range(max_retries):
            next_v = await self.next_version_for(signal_id)
            decision = decision_factory(next_v)
            if decision.signal_id != signal_id:
                raise ValueError(
                    f"factory produced decision for signal_id={decision.signal_id}, "
                    f"expected {signal_id}"
                )
            try:
                await self.add(decision)
                return decision
            except RiskDecisionVersionMismatchError as exc:
                last_err = exc
                continue
        # Out of retries: surface the last conflict.
        assert last_err is not None
        raise last_err

    # ─── Reads ─────────────────────────────────────────────────────

    async def next_version_for(self, signal_id: int) -> int:
        async with self._sf() as session:
            return await self._next_version_in_session(session, signal_id)

    async def list_for_signal(self, signal_id: int) -> list[RiskDecision]:
        async with self._sf() as session:
            stmt = (
                select(RiskDecisionRow)
                .where(RiskDecisionRow.signal_id == signal_id)
                .order_by(RiskDecisionRow.version.asc())
            )
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def latest_for_signal(self, signal_id: int) -> RiskDecision | None:
        async with self._sf() as session:
            stmt = (
                select(RiskDecisionRow)
                .where(RiskDecisionRow.signal_id == signal_id)
                .order_by(RiskDecisionRow.version.desc())
                .limit(1)
            )
            row = (await session.scalars(stmt)).first()
            return _from_row(row) if row is not None else None

    # ─── Internal ──────────────────────────────────────────────────

    async def _next_version_in_session(
        self, session: AsyncSession, signal_id: int
    ) -> int:
        stmt = select(func.count(RiskDecisionRow.id)).where(
            RiskDecisionRow.signal_id == signal_id
        )
        existing = (await session.scalar(stmt)) or 0
        return int(existing) + 1


# ─── Translation ───────────────────────────────────────────────────

def _to_row(decision: RiskDecision) -> RiskDecisionRow:
    decided_at = decision.decided_at
    # SQLite DateTime is naive; strip tzinfo for storage consistency.
    if decided_at.tzinfo is not None:
        decided_at = decided_at.astimezone(UTC).replace(tzinfo=None)
    return RiskDecisionRow(
        signal_id=decision.signal_id,
        version=decision.version,
        approved=decision.approved,
        gate_results_json=[asdict(r) for r in decision.gate_results],
        sized_amount_quote=decision.sized_amount,
        reasoning=decision.reasoning,
        decided_at=decided_at,
    )


def _from_row(row: RiskDecisionRow) -> RiskDecision:
    raw_gates: list[dict[str, Any]] = list(row.gate_results_json or [])
    gates: tuple[GateResult, ...] = tuple(
        GateResult(
            passed=bool(g.get("passed", False)),
            reason=str(g.get("reason", "")),
            gate_name=str(g.get("gate_name", "")),
        )
        for g in raw_gates
    )
    sized: Decimal | None = (
        Decimal(str(row.sized_amount_quote))
        if row.sized_amount_quote is not None
        else None
    )
    decided_at = row.decided_at
    if decided_at.tzinfo is None:
        decided_at = decided_at.replace(tzinfo=UTC)
    return RiskDecision(
        signal_id=row.signal_id,
        version=row.version,
        approved=row.approved,
        gate_results=gates,
        reasoning=row.reasoning,
        decided_at=decided_at,
        sized_amount=sized,
    )
