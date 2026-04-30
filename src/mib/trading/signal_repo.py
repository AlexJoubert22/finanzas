"""Persistence boundary for :class:`mib.trading.signals.Signal`.

The repository owns the translation between the in-memory thesis
(``Signal``, frozen dataclass) and its DB row (``SignalRow`` ORM).
Callers outside this module never touch ``SignalRow`` directly — they
hand in a ``Signal`` to :meth:`SignalRepository.add` and receive
``PersistedSignal`` back from any read.

# Append-only mandate (ROADMAP.md Parte 0): same pattern will apply to
# trades (FASE 9), risk_decisions (FASE 8.6), orders (FASE 9). Born
# append-only from day one — every status mutation goes through
# :meth:`transition`, which writes to ``signal_status_events`` and
# updates the ``signals.status`` cache atomically inside one
# transaction. Direct ``UPDATE`` on ``signals.status`` from business
# code is forbidden.

Mypy strict is intentionally NOT applied to this module: SQLAlchemy
``Mapped[T]`` inference and dynamic query building fight with strict
without catching proportional bugs. Strict lives on the pure
``mib.trading.signals`` types instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mib.db.models import SignalRow, SignalStatusEvent
from mib.logger import logger
from mib.trading.signals import (
    SIGNAL_STATUSES,
    PersistedSignal,
    Side,
    Signal,
    SignalStatus,
)

# Timeframe → seconds. Used to compute the default TTL
# (``expires_at = generated_at + ttl_bars * timeframe_seconds``).
# Conservative coverage of the timeframes the StrategyEngine emits.
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1wk": 604800,
}

#: Default TTL multiplier on top of the strategy's timeframe.
#: 4 candles is the FASE 8.1 default (e.g. 1h timeframe → 4h TTL).
DEFAULT_TTL_BARS: int = 4


class StaleSignalStateError(Exception):
    """Raised when :meth:`SignalRepository.transition` is called with
    ``expected_from_status`` that does not match the row's current
    state. Indicates that another actor (TTL job, parallel callback,
    reconciler) transitioned the signal between the caller's read and
    this transaction. The caller can re-read and decide.
    """

    def __init__(self, signal_id: int, expected: str, actual: str) -> None:
        super().__init__(
            f"signal #{signal_id}: expected from_status={expected!r}, "
            f"got {actual!r}"
        )
        self.signal_id = signal_id
        self.expected = expected
        self.actual = actual


def _compute_expires_at(generated_at: datetime, timeframe: str, ttl_bars: int) -> datetime | None:
    """Return ``generated_at + ttl_bars × timeframe_seconds``, or None
    if the timeframe is not recognised (caller-side fall-back: no TTL
    set, signal never auto-expires).
    """
    seconds = _TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        logger.warning(
            "signal_repo: unrecognised timeframe {!r}, expires_at left NULL",
            timeframe,
        )
        return None
    return generated_at + timedelta(seconds=seconds * ttl_bars)


class SignalRepository:
    """CRUD for the ``signals`` table, dataclass-in / dataclass-out."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ─── Writes ────────────────────────────────────────────────────

    async def add(
        self,
        signal: Signal,
        *,
        ttl_bars: int = DEFAULT_TTL_BARS,
    ) -> PersistedSignal:
        """Persist ``signal`` as a new row with status='pending'.

        Computes ``expires_at = generated_at + ttl_bars × timeframe_seconds``
        and writes a 'created' event in the same transaction so the
        audit trail is complete from row birth.
        """
        if ttl_bars <= 0:
            raise ValueError(f"ttl_bars must be > 0 (got {ttl_bars})")

        expires_at = _compute_expires_at(signal.generated_at, signal.timeframe, ttl_bars)

        async with self._sf() as session:
            async with session.begin():
                row = _to_row(
                    signal,
                    status="pending",
                    status_updated_at=signal.generated_at,
                    expires_at=expires_at,
                )
                session.add(row)
                # Flush so row.id is populated for the FK on the event row.
                await session.flush()
                event = SignalStatusEvent(
                    signal_id=row.id,
                    from_status=None,  # creation has no prior state
                    to_status="pending",
                    event_type="created",
                    actor="system",
                    reason=None,
                    metadata_json=None,
                )
                session.add(event)
            await session.refresh(row)
            logger.debug(
                "signal_repo: added id={} strategy={} ticker={} expires_at={}",
                row.id,
                row.strategy_id,
                row.ticker,
                row.expires_at,
            )
            return _from_row(row)

    async def transition(
        self,
        signal_id: int,
        to_status: SignalStatus,
        *,
        actor: str,
        event_type: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_from_status: SignalStatus | None = None,
    ) -> PersistedSignal | None:
        """Atomic status transition with audit event.

        Performs in a single transaction:

        1. Read current ``signals.status`` for the row.
        2. If ``expected_from_status`` is given and does not match,
           raise :class:`StaleSignalStateError`.
        3. Insert a row in ``signal_status_events`` with the
           transition details.
        4. Update ``signals.status`` and ``signals.status_updated_at``.

        Returns the new :class:`PersistedSignal`, or ``None`` if the
        ``signal_id`` does not exist. SQLite WAL mode (already set
        globally) gives us the serial-write semantics needed for race
        safety: two concurrent ``transition`` calls on the same row
        will serialize at commit time, with the second seeing the
        first's effect.

        Raises:
            ValueError: if ``to_status`` or ``expected_from_status``
                is not a valid :data:`SIGNAL_STATUSES` member.
            StaleSignalStateError: if ``expected_from_status`` is set
                and does not match the row's current status.
        """
        if to_status not in SIGNAL_STATUSES:
            raise ValueError(f"invalid SignalStatus: {to_status!r}")
        if (
            expected_from_status is not None
            and expected_from_status not in SIGNAL_STATUSES
        ):
            raise ValueError(
                f"invalid expected SignalStatus: {expected_from_status!r}"
            )

        async with self._sf() as session:
            # SQLite race protection: ``BEGIN IMMEDIATE`` acquires the
            # write lock at transaction start rather than at first
            # write. Without this, two concurrent transitions on the
            # same signal can both pass the ``expected_from_status``
            # check before either commits, defeating the race-safety
            # contract. With IMMEDIATE, the second caller blocks until
            # the first commits, then sees the updated status and
            # raises StaleSignalStateError.
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                row = await session.get(SignalRow, signal_id)
                if row is None:
                    await session.rollback()
                    return None

                current = row.status
                if (
                    expected_from_status is not None
                    and current != expected_from_status
                ):
                    await session.rollback()
                    raise StaleSignalStateError(
                        signal_id, expected_from_status, current
                    )

                now = datetime.now().astimezone()
                event = SignalStatusEvent(
                    signal_id=signal_id,
                    from_status=current,
                    to_status=to_status,
                    event_type=event_type,
                    actor=actor,
                    reason=reason,
                    metadata_json=metadata,
                )
                session.add(event)

                row.status = to_status
                row.status_updated_at = now

                await session.commit()
            except StaleSignalStateError:
                raise
            except Exception:
                await session.rollback()
                raise

            await session.refresh(row)
            logger.debug(
                "signal_repo: transitioned id={} {}->{} actor={} event={}",
                signal_id,
                current,
                to_status,
                actor,
                event_type,
            )
            return _from_row(row)

    # ─── Reads ─────────────────────────────────────────────────────

    async def get(self, signal_id: int) -> PersistedSignal | None:
        async with self._sf() as session:
            row = await session.get(SignalRow, signal_id)
            return _from_row(row) if row is not None else None

    async def list_pending(self, *, limit: int = 100) -> list[PersistedSignal]:
        async with self._sf() as session:
            stmt = (
                select(SignalRow)
                .where(SignalRow.status == "pending")
                .order_by(SignalRow.generated_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def list_by_ticker_and_status(
        self,
        ticker: str,
        status: SignalStatus,
        *,
        limit: int = 100,
    ) -> list[PersistedSignal]:
        """Return signals matching ``ticker`` and ``status``.

        Used by FASE 8.4a exposure gate to find approved-but-unexecuted
        signals whose sized amount counts against the per-ticker cap.
        """
        if status not in SIGNAL_STATUSES:
            raise ValueError(f"invalid SignalStatus: {status!r}")
        async with self._sf() as session:
            stmt = (
                select(SignalRow)
                .where(SignalRow.ticker == ticker, SignalRow.status == status)
                .order_by(SignalRow.generated_at.desc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def list_by_strategy(
        self,
        strategy_id: str,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[PersistedSignal]:
        async with self._sf() as session:
            stmt = select(SignalRow).where(SignalRow.strategy_id == strategy_id)
            if since is not None:
                stmt = stmt.where(SignalRow.generated_at >= since)
            stmt = stmt.order_by(SignalRow.generated_at.desc()).limit(limit)
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def list_expired_pending(
        self, *, now: datetime | None = None, limit: int = 500
    ) -> list[PersistedSignal]:
        """Return pending signals whose ``expires_at`` is in the past.

        Used by :func:`mib.trading.expiration.expire_stale_signals_job`
        to identify candidates for transition to ``expired``.
        """
        cutoff = now if now is not None else datetime.now().astimezone()
        async with self._sf() as session:
            stmt = (
                select(SignalRow)
                .where(
                    SignalRow.status == "pending",
                    SignalRow.expires_at.is_not(None),
                    SignalRow.expires_at < cutoff,
                )
                .order_by(SignalRow.expires_at.asc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return [_from_row(r) for r in rows]

    async def list_events(
        self, signal_id: int, *, limit: int = 100
    ) -> list[SignalStatusEvent]:
        """Return the audit trail for a signal, oldest first.

        Returns ORM rows directly (audit trail is internal/diagnostic
        surface; there is no public dataclass to translate to).
        """
        async with self._sf() as session:
            stmt = (
                select(SignalStatusEvent)
                .where(SignalStatusEvent.signal_id == signal_id)
                .order_by(SignalStatusEvent.created_at.asc(), SignalStatusEvent.id.asc())
                .limit(limit)
            )
            rows = (await session.scalars(stmt)).all()
            return list(rows)


# ─── Translation helpers ───────────────────────────────────────────

def _to_row(
    signal: Signal,
    *,
    status: SignalStatus,
    status_updated_at: datetime,
    expires_at: datetime | None,
) -> SignalRow:
    low, high = signal.entry_zone
    indicators_payload: dict[str, object] = dict(signal.indicators)
    return SignalRow(
        ticker=signal.ticker,
        side=signal.side,
        strength=signal.strength,
        timeframe=signal.timeframe,
        entry_low=low,
        entry_high=high,
        invalidation=signal.invalidation,
        target_1=signal.target_1,
        target_2=signal.target_2,
        rationale=signal.rationale,
        indicators_json=indicators_payload,
        generated_at=signal.generated_at,
        strategy_id=signal.strategy_id,
        confidence_ai=signal.confidence_ai,
        status=status,
        status_updated_at=status_updated_at,
        expires_at=expires_at,
    )


def _from_row(row: SignalRow) -> PersistedSignal:
    raw_ind: dict[str, object] = row.indicators_json or {}
    indicators: dict[str, float] = {
        k: float(v) for k, v in raw_ind.items() if isinstance(v, (int, float))
    }
    side: Side = _coerce_side(row.side)
    status: SignalStatus = _coerce_status(row.status)
    signal = Signal(
        ticker=row.ticker,
        side=side,
        strength=row.strength,
        timeframe=row.timeframe,
        entry_zone=(row.entry_low, row.entry_high),
        invalidation=row.invalidation,
        target_1=row.target_1,
        target_2=row.target_2,
        rationale=row.rationale,
        indicators=indicators,
        generated_at=row.generated_at,
        strategy_id=row.strategy_id,
        confidence_ai=row.confidence_ai,
    )
    return PersistedSignal(
        id=row.id,
        status=status,
        signal=signal,
        status_updated_at=row.status_updated_at,
    )


def _coerce_side(raw: str) -> Side:
    if raw not in ("long", "short", "flat"):
        raise ValueError(f"unexpected side in DB row: {raw!r}")
    return raw  # type: ignore[return-value]


def _coerce_status(raw: str) -> SignalStatus:
    if raw not in SIGNAL_STATUSES:
        raise ValueError(f"unexpected status in DB row: {raw!r}")
    return raw
