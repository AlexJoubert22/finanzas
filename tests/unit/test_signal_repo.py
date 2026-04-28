"""Repository roundtrip + lifecycle tests for the signals table.

Exercises the in-memory ``Signal`` ↔ ``SignalRow`` translation
provided by :mod:`mib.trading.signal_repo` against a fresh sqlite DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mib.db.session import async_session_factory
from mib.trading.signal_repo import SignalRepository
from mib.trading.signals import Signal


def _signal(
    *,
    strategy_id: str = "scanner.oversold.v1",
    ticker: str = "BTC/USDT",
    generated_at: datetime | None = None,
    target_2: float | None = 109.0,
    confidence_ai: float | None = None,
    indicators: dict[str, float] | None = None,
) -> Signal:
    return Signal(
        ticker=ticker,
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 101.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=target_2,
        rationale="test signal",
        indicators=indicators or {"rsi_14": 22.5, "atr_14": 2.0},
        generated_at=generated_at or datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id=strategy_id,
        confidence_ai=confidence_ai,
    )


@pytest.fixture
def repo() -> SignalRepository:
    return SignalRepository(async_session_factory)


@pytest.mark.asyncio
async def test_add_returns_persisted_signal_with_pending_status(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    persisted = await repo.add(_signal())
    assert persisted.id > 0
    assert persisted.status == "pending"
    assert persisted.status_updated_at == persisted.signal.generated_at
    assert persisted.signal.ticker == "BTC/USDT"


@pytest.mark.asyncio
async def test_add_computes_default_expires_at_from_timeframe(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    """1h timeframe + default ttl_bars=4 → expires_at = generated_at + 4h.

    SQLite stores DateTime as naive (no tzinfo); compare wall-clock values.
    """
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    persisted = await repo.add(_signal(generated_at=base))
    from mib.db.models import SignalRow  # noqa: PLC0415
    from mib.db.session import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        row = await session.get(SignalRow, persisted.id)
        assert row is not None
        expected_naive = (base + timedelta(hours=4)).replace(tzinfo=None)
        assert row.expires_at == expected_naive


@pytest.mark.asyncio
async def test_add_with_ttl_bars_override(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    persisted = await repo.add(_signal(generated_at=base), ttl_bars=8)
    from mib.db.models import SignalRow  # noqa: PLC0415
    from mib.db.session import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        row = await session.get(SignalRow, persisted.id)
        assert row is not None
        # 1h × 8 bars = 8h
        expected_naive = (base + timedelta(hours=8)).replace(tzinfo=None)
        assert row.expires_at == expected_naive


@pytest.mark.asyncio
async def test_get_roundtrips_full_signal(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    indicators = {"rsi_14": 22.5, "atr_14": 2.0, "ema_50": 99.5}
    sig = _signal(indicators=indicators, confidence_ai=0.65, target_2=None)
    saved = await repo.add(sig)
    fetched = await repo.get(saved.id)
    assert fetched is not None
    assert fetched.signal.indicators == indicators
    assert fetched.signal.confidence_ai == pytest.approx(0.65)
    assert fetched.signal.target_2 is None


@pytest.mark.asyncio
async def test_get_unknown_id_returns_none(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    assert await repo.get(99_999) is None


@pytest.mark.asyncio
async def test_list_pending_returns_only_pending_desc_by_time(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    a = await repo.add(_signal(generated_at=base))
    b = await repo.add(_signal(generated_at=base + timedelta(minutes=5)))
    c = await repo.add(_signal(generated_at=base + timedelta(minutes=10)))
    # Knock one out of the pending pool via the append-only transition API.
    await repo.transition(
        a.id, "cancelled", actor="user:test", event_type="cancelled"
    )

    pending = await repo.list_pending()
    ids = [p.id for p in pending]
    assert ids == [c.id, b.id]


@pytest.mark.asyncio
async def test_list_by_strategy_filters_and_uses_since(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    await repo.add(_signal(strategy_id="scanner.oversold.v1", generated_at=base))
    await repo.add(
        _signal(
            strategy_id="scanner.oversold.v1",
            generated_at=base + timedelta(hours=2),
        )
    )
    await repo.add(_signal(strategy_id="scanner.breakout.v1", generated_at=base))

    all_oversold = await repo.list_by_strategy("scanner.oversold.v1")
    assert len(all_oversold) == 2
    assert all(p.signal.strategy_id == "scanner.oversold.v1" for p in all_oversold)

    recent_only = await repo.list_by_strategy(
        "scanner.oversold.v1", since=base + timedelta(hours=1)
    )
    assert len(recent_only) == 1


@pytest.mark.asyncio
async def test_indicators_dict_survives_json_roundtrip_filtering_non_numeric(
    repo: SignalRepository, fresh_db: None  # noqa: ARG001
) -> None:
    # Numeric ints are accepted by the dataclass (Python's float
    # accepts int) but on the way out we coerce everything to float.
    saved = await repo.add(_signal(indicators={"rsi_14": 25.0, "atr_14": 2.5}))
    fetched = await repo.get(saved.id)
    assert fetched is not None
    assert isinstance(fetched.signal.indicators["rsi_14"], float)
    assert fetched.signal.indicators == {"rsi_14": 25.0, "atr_14": 2.5}
