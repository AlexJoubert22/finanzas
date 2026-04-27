"""Coordinator tests for ``scanner_to_signals_job``.

The job must:

- Run StrategyEngine, persist every signal via repo, attempt to ship
  each to Telegram.
- Treat Telegram as best-effort — a failed send must NOT roll back
  the persisted row. The signal stays ``pending`` so /signals pending
  recovers it.
- Tolerate a per-signal repo failure (skip that one, keep going).
- Return the count of persisted rows, regardless of how many Telegram
  messages succeeded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from mib.trading import notify as notify_mod
from mib.trading.signals import PersistedSignal, Signal


def _signal(ticker: str = "BTC/USDT", strategy_id: str = "scanner.oversold.v1") -> Signal:
    return Signal(
        ticker=ticker,
        side="long",
        strength=0.7,
        timeframe="1h",
        entry_zone=(100.0, 100.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=109.0,
        rationale="test",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id=strategy_id,
        confidence_ai=None,
    )


class _FakeEngine:
    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    async def run(self, preset: str, tickers: list[str], **kwargs: Any) -> list[Signal]:
        return list(self._signals)


class _FakeRepo:
    def __init__(self, *, raise_on_index: int | None = None) -> None:
        self._next_id = 1
        self._raise_on_index = raise_on_index
        self._call_count = 0
        self.added: list[Signal] = []

    async def add(self, signal: Signal) -> PersistedSignal:
        if self._raise_on_index is not None and self._call_count == self._raise_on_index:
            self._call_count += 1
            raise RuntimeError("simulated DB outage")
        self._call_count += 1
        self.added.append(signal)
        pid = self._next_id
        self._next_id += 1
        return PersistedSignal(
            id=pid,
            status="pending",
            signal=signal,
            status_updated_at=signal.generated_at,
        )


class _FakeBot:
    def __init__(self, *, fail_each_call: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail_each_call

    async def send_message(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("telegram unreachable")


class _FakeApp:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch, *, engine: _FakeEngine, repo: _FakeRepo
) -> None:
    monkeypatch.setattr(notify_mod, "get_strategy_engine", lambda: engine)
    monkeypatch.setattr(notify_mod, "get_signal_repository", lambda: repo)


@pytest.mark.asyncio
async def test_zero_signals_zero_telegram_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine([])
    repo = _FakeRepo()
    bot = _FakeBot()
    _patch_deps(monkeypatch, engine=engine, repo=repo)

    count = await notify_mod.scanner_to_signals_job(
        _FakeApp(bot),  # type: ignore[arg-type]
        preset="oversold",
        tickers=["BTC/USDT"],
        notify_chat_id=42,
    )
    assert count == 0
    assert bot.calls == []


@pytest.mark.asyncio
async def test_each_signal_persisted_and_shipped_to_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine([_signal("BTC/USDT"), _signal("ETH/USDT")])
    repo = _FakeRepo()
    bot = _FakeBot()
    _patch_deps(monkeypatch, engine=engine, repo=repo)

    count = await notify_mod.scanner_to_signals_job(
        _FakeApp(bot),  # type: ignore[arg-type]
        preset="oversold",
        tickers=["BTC/USDT", "ETH/USDT"],
        notify_chat_id=42,
    )
    assert count == 2
    assert len(repo.added) == 2
    assert len(bot.calls) == 2
    assert all(c["chat_id"] == 42 for c in bot.calls)
    # Each call carries the keyboard.
    assert all(c.get("reply_markup") is not None for c in bot.calls)


@pytest.mark.asyncio
async def test_telegram_failure_does_not_rollback_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-negotiable: persisted signals stay in DB even when
    every Telegram send fails. /signals pending recovers them later.
    """
    engine = _FakeEngine([_signal()])
    repo = _FakeRepo()
    bot = _FakeBot(fail_each_call=True)
    _patch_deps(monkeypatch, engine=engine, repo=repo)

    count = await notify_mod.scanner_to_signals_job(
        _FakeApp(bot),  # type: ignore[arg-type]
        preset="oversold",
        tickers=["BTC/USDT"],
        notify_chat_id=42,
    )
    # Telegram send raised but the signal is still persisted.
    assert count == 1
    assert len(repo.added) == 1


@pytest.mark.asyncio
async def test_per_signal_persist_failure_skips_just_that_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _FakeEngine([_signal("BTC/USDT"), _signal("ETH/USDT")])
    repo = _FakeRepo(raise_on_index=0)  # first persist throws
    bot = _FakeBot()
    _patch_deps(monkeypatch, engine=engine, repo=repo)

    count = await notify_mod.scanner_to_signals_job(
        _FakeApp(bot),  # type: ignore[arg-type]
        preset="oversold",
        tickers=["BTC/USDT", "ETH/USDT"],
        notify_chat_id=42,
    )
    # Second signal persisted + notified; first signal silently skipped.
    assert count == 1
    assert [s.ticker for s in repo.added] == ["ETH/USDT"]
    assert len(bot.calls) == 1


def test_signal_keyboard_callback_data_is_well_under_64_bytes() -> None:
    kb = notify_mod.signal_keyboard(99_999)
    # The keyboard is a 1×3 row; the largest expected ID for the
    # foreseeable future fits comfortably in the Telegram limit.
    for row in kb.inline_keyboard:
        for button in row:
            assert button.callback_data is not None
            assert len(button.callback_data.encode("utf-8")) <= 64
