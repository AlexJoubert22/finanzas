"""Unit tests for the indicator snapshot wrapper."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mib.indicators.technical import compute_snapshot


@pytest.fixture
def btc_ohlcv() -> pd.DataFrame:
    """Deterministic 200-bar fixture matching scripts/validate_pandas_ta.py."""
    rng = np.random.default_rng(42)
    n = 200
    steps = rng.normal(loc=50.0, scale=400.0, size=n).cumsum()
    close = 60000.0 + steps
    open_ = close + rng.normal(0.0, 100.0, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(80.0, 50.0, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(80.0, 50.0, size=n))
    volume = rng.uniform(100.0, 1000.0, size=n)
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_compute_snapshot_returns_all_fields(btc_ohlcv: pd.DataFrame) -> None:
    snap = compute_snapshot(btc_ohlcv)
    # These values were cross-checked against ta (Bukosabino) inside
    # scripts/validate_pandas_ta.py; see that file for the tolerance
    # reasoning.
    assert snap.rsi_14 is not None and 0.0 <= snap.rsi_14 <= 100.0
    assert snap.macd is not None
    assert snap.macd_signal is not None
    assert snap.macd_hist is not None
    assert snap.ema_20 is not None
    assert snap.ema_50 is not None
    assert snap.ema_200 is not None
    # Bollinger must keep the v0.3 public names even though pandas-ta 0.4
    # underneath writes BBL_20_2.0_2.0 etc.
    assert snap.bb_lower is not None and snap.bb_middle is not None
    assert snap.bb_upper is not None
    assert snap.bb_lower < snap.bb_middle < snap.bb_upper
    assert snap.adx_14 is not None and snap.adx_14 >= 0.0
    # ATR is a strictly positive distance — anything else means pandas-ta
    # changed semantics or our adapter dropped the column. The FASE 7
    # strategy engine relies on this being usable to derive stops.
    assert snap.atr_14 is not None
    assert snap.atr_14 > 0.0


def test_compute_snapshot_with_insufficient_history() -> None:
    # Only 10 bars — EMA-200 and ADX-14 cannot be computed.
    n = 10
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [500.0] * n,
        },
        index=idx,
    )
    snap = compute_snapshot(df)
    # Degenerate input — every field should either be None or a finite number.
    for f in (
        snap.rsi_14,
        snap.macd,
        snap.ema_200,
        snap.bb_lower,
        snap.atr_14,
        snap.adx_14,
    ):
        assert f is None or (isinstance(f, float) and f == f)  # f == f → not NaN
