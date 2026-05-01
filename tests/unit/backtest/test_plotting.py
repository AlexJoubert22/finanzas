"""Tests for :mod:`mib.backtest.plotting` (FASE 12.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mib.backtest.equity import EquityPoint
from mib.backtest.plotting import render_equity_curve_png

#: PNG magic header — first 8 bytes of any valid PNG.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _curve(n: int = 5) -> list[EquityPoint]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    pts: list[EquityPoint] = []
    eq = Decimal("1000")
    for i in range(n):
        eq += Decimal(10)
        pts.append(
            EquityPoint(
                timestamp=base + timedelta(hours=i),
                equity_with_fees=eq - Decimal(2 * i),
                equity_without_fees=eq,
                realized_pnl_cumulative=eq - Decimal(1000),
                fees_cumulative=Decimal(2 * i),
            )
        )
    return pts


def test_render_returns_png_magic_bytes() -> None:
    png = render_equity_curve_png(_curve())
    assert png[:8] == _PNG_MAGIC


def test_render_size_reasonable() -> None:
    png = render_equity_curve_png(_curve(50))
    # 50-point curve at 1200x600 dpi 100 should easily fit under 200KB.
    assert 1_000 < len(png) < 200_000


def test_render_empty_curve_produces_placeholder_png() -> None:
    """Empty curve still yields valid PNG bytes — operator gets a
    diagnosable image rather than an empty body / 500.
    """
    png = render_equity_curve_png([])
    assert png[:8] == _PNG_MAGIC
    assert len(png) > 100
