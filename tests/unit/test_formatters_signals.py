"""Smoke tests for the FASE 7 Signal-related Telegram formatters."""

from __future__ import annotations

from datetime import UTC, datetime

from mib.telegram.formatters import (
    fmt_pending_signals_list,
    fmt_signal_card,
)
from mib.trading.signals import PersistedSignal, Signal


def _persisted(*, target_2: float | None = 109.0, signal_id: int = 7) -> PersistedSignal:
    sig = Signal(
        ticker="BTC/USDT",
        side="long",
        strength=0.72,
        timeframe="1h",
        entry_zone=(100.0, 100.0),
        invalidation=97.0,
        target_1=103.0,
        target_2=target_2,
        rationale="RSI=22.0 (<30), vol/avg20=1.8x",
        indicators={"rsi_14": 22.0, "atr_14": 2.0},
        generated_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        strategy_id="scanner.oversold.v1",
        confidence_ai=None,
    )
    return PersistedSignal(
        id=signal_id, status="pending", signal=sig, status_updated_at=sig.generated_at
    )


def test_signal_card_includes_all_levels_and_id() -> None:
    body = fmt_signal_card(_persisted())
    assert "scanner.oversold.v1" in body
    assert "BTC/USDT" in body
    assert "Stop:" in body
    assert "T1 (1R):" in body
    assert "T2" in body
    assert "#7" in body
    assert "pending" in body
    # Long signals display the green emoji, never the red one.
    assert "🟢" in body
    assert "🔴" not in body


def test_signal_card_drops_t2_line_when_none() -> None:
    body = fmt_signal_card(_persisted(target_2=None))
    assert "T2" not in body


def test_signal_card_can_hide_id_for_terminal_state() -> None:
    body = fmt_signal_card(_persisted(), include_id=False)
    assert "#7" not in body


def test_pending_list_empty_message() -> None:
    assert fmt_pending_signals_list([]) == "No hay signals pendientes."


def test_pending_list_renders_one_line_per_signal() -> None:
    body = fmt_pending_signals_list([_persisted(signal_id=1), _persisted(signal_id=2)])
    assert "(2)" in body
    assert "#1" in body
    assert "#2" in body
