"""Technical indicators wrappers over pandas-ta 0.4.71b.

The sole reason for this thin adapter layer is the Bollinger Bands
breaking change detected by ``scripts/validate_pandas_ta.py``:

    pandas-ta 0.3.x:  BBL_20_2.0  / BBM_20_2.0  / BBU_20_2.0
    pandas-ta 0.4.x:  BBL_20_2.0_2.0 / BBM_20_2.0_2.0 / BBU_20_2.0_2.0

Public API keeps the 0.3 names so callers (and the Pydantic schema
``TechnicalSnapshot``) stay stable across pandas-ta versions.

Inputs: a ``pandas.DataFrame`` with ``open``, ``high``, ``low``,
``close``, ``volume`` columns indexed by timestamp.

Outputs: a ``TechnicalSnapshot`` Pydantic model holding the most recent
value of every indicator (not the full series — /symbol only needs
the latest bar).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pandas_ta as pta  # type: ignore[import-untyped]

from mib.models.market import TechnicalSnapshot


def compute_snapshot(df: pd.DataFrame) -> TechnicalSnapshot:
    """Compute RSI/MACD/EMA/Bollinger/ADX on the last bar of ``df``.

    Values that cannot be produced (insufficient history, all-NaN column)
    are returned as ``None`` so the caller can choose to render "—" in
    the UI without crashing.
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi = pta.rsi(close, length=14)
    macd_df = pta.macd(close, fast=12, slow=26, signal=9)
    ema20 = pta.ema(close, length=20)
    ema50 = pta.ema(close, length=50)
    ema200 = pta.ema(close, length=200)
    bb_df = pta.bbands(close, length=20, std=2)
    adx_df = pta.adx(high, low, close, length=14)

    return TechnicalSnapshot(
        rsi_14=_safe_last(rsi),
        macd=_safe_last(macd_df, col="MACD_12_26_9"),
        macd_signal=_safe_last(macd_df, col="MACDs_12_26_9"),
        macd_hist=_safe_last(macd_df, col="MACDh_12_26_9"),
        ema_20=_safe_last(ema20),
        ema_50=_safe_last(ema50),
        ema_200=_safe_last(ema200),
        # Bollinger — map 0.4 columns back to 0.3 names publicly.
        bb_lower=_safe_last(bb_df, col="BBL_20_2.0_2.0"),
        bb_middle=_safe_last(bb_df, col="BBM_20_2.0_2.0"),
        bb_upper=_safe_last(bb_df, col="BBU_20_2.0_2.0"),
        adx_14=_safe_last(adx_df, col="ADX_14"),
    )


def _safe_last(obj: Any, col: str | None = None) -> float | None:
    """Return the last non-NaN value of a Series/DataFrame column.

    Handles the degenerate cases pandas-ta can produce (empty frame,
    all-NaN result when history is too short for the indicator).
    """
    if obj is None:
        return None
    series = obj[col] if col is not None and isinstance(obj, pd.DataFrame) else obj
    try:
        s = pd.Series(series).dropna()
        if len(s) == 0:
            return None
        return float(s.iloc[-1])
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["compute_snapshot"]
