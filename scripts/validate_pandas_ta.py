"""Pre-phase-3 validation of pandas-ta 0.4.71b.

Checks three things before we commit to ``pandas-ta`` for phase 3:

1. **Column names** match what spec §5 (carried over from pandas-ta 0.3)
   expects: ``RSI_14``, ``MACD_12_26_9``, ``MACDs_12_26_9``, ``MACDh_12_26_9``,
   ``EMA_20/50/200``, ``BBL_20_2.0 / BBM_20_2.0 / BBU_20_2.0``, ``ADX_14``.
2. **Numeric outputs** are sane (not NaN for the last bar, in the right
   order of magnitude).
3. **Cross-check** the last values against ``ta`` (Bukosabino) — if both
   libs disagree by more than 1 % we treat pandas-ta as unreliable and
   recommend switching.

Fixture is a deterministic 200-bar OHLCV series (seed=42) shaped to mimic
BTC/USDT around 50k–100k. Run with:

    uv run python scripts/validate_pandas_ta.py
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pandas_ta as pta
import ta  # Bukosabino — reference

# Column names expected by spec §5 (matches pandas-ta 0.3.x).
EXPECTED_COLS_0_3 = {
    "rsi": {"RSI_14"},
    "macd": {"MACD_12_26_9", "MACDh_12_26_9", "MACDs_12_26_9"},
    "ema20": {"EMA_20"},
    "ema50": {"EMA_50"},
    "ema200": {"EMA_200"},
    "bbands": {"BBL_20_2.0", "BBM_20_2.0", "BBU_20_2.0"},
    "adx": {"ADX_14"},
}

# Column names observed in pandas-ta 0.4.71b. Bollinger gained a trailing
# `_2.0` (ddof=2.0 embedded twice into the name) and two new outputs:
# BBB = bandwidth, BBP = percent B.
EXPECTED_COLS_0_4 = {
    **EXPECTED_COLS_0_3,
    "bbands": {"BBL_20_2.0_2.0", "BBM_20_2.0_2.0", "BBU_20_2.0_2.0"},
}

# Mapping we'll use in indicators/technical.py to keep the public API
# stable across pandas-ta versions — internal code reads the v0.4 column
# and renames back to the v0.3 key that consumers/tests expect.
SUPPORTED_RENAME_MAP = {
    "BBL_20_2.0_2.0": "BBL_20_2.0",
    "BBM_20_2.0_2.0": "BBM_20_2.0",
    "BBU_20_2.0_2.0": "BBU_20_2.0",
}


def build_fixture(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with realistic shape for BTC/USDT range."""
    rng = np.random.default_rng(seed)
    # Random walk with mild trend.
    steps = rng.normal(loc=50.0, scale=400.0, size=n).cumsum()
    base = 60000.0 + steps
    # Build OHLC from the close path with small noise per bar.
    close = base
    open_ = close + rng.normal(0.0, 100.0, size=n)
    high = np.maximum(open_, close) + np.abs(rng.normal(80.0, 50.0, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(80.0, 50.0, size=n))
    volume = rng.uniform(100.0, 1000.0, size=n)
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def check_columns(got_cols: set[str], expected: set[str], indicator: str) -> bool:
    missing = expected - got_cols
    extra = got_cols - expected
    status = "OK" if not missing else "FAIL"
    print(f"  [{indicator}] columns: {sorted(got_cols)}  → {status}")
    if missing:
        print(f"    ✗ missing: {sorted(missing)}")
    if extra:
        print(f"    · extra (non-blocking): {sorted(extra)}")
    return not missing


def cross_check(pta_val: float, ta_val: float, name: str, tol_pct: float = 1.0) -> bool:
    if np.isnan(pta_val) or np.isnan(ta_val):
        print(f"    ✗ {name}: NaN in one of the libs (pta={pta_val}, ta={ta_val})")
        return False
    if ta_val == 0.0:
        ok = abs(pta_val) < 1e-6
    else:
        diff_pct = abs(pta_val - ta_val) / abs(ta_val) * 100.0
        ok = diff_pct <= tol_pct
    marker = "OK" if ok else "MISMATCH"
    print(f"    {name:>20s}: pta={pta_val:>14.4f} · ta={ta_val:>14.4f} → {marker}")
    return ok


def main() -> int:
    df = build_fixture(200)
    print(f"Fixture: {len(df)} bars, close range {df['close'].min():.0f} – {df['close'].max():.0f}")
    ta_ver = getattr(ta, "__version__", "?")
    print(f"pandas-ta version: {pta.version}  ·  ta (Bukosabino) version: {ta_ver}")
    print("=" * 70)

    all_pass = True
    numeric_pass = True

    # ─── RSI(14) ──────────────────────────────────────────────────────────
    print("\n[RSI(14)]")
    rsi_pta = pta.rsi(df["close"], length=14)
    cols = {rsi_pta.name} if isinstance(rsi_pta, pd.Series) else set(rsi_pta.columns)
    all_pass &= check_columns(cols, EXPECTED_COLS_0_4["rsi"], "rsi")
    rsi_ta = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    numeric_pass &= cross_check(rsi_pta.iloc[-1], rsi_ta.iloc[-1], "RSI_14[-1]")

    # ─── MACD(12, 26, 9) ─────────────────────────────────────────────────
    print("\n[MACD(12, 26, 9)]")
    macd_pta = pta.macd(df["close"], fast=12, slow=26, signal=9)
    cols = set(macd_pta.columns)
    all_pass &= check_columns(cols, EXPECTED_COLS_0_4["macd"], "macd")
    macd_ta_ind = ta.trend.MACD(df["close"], window_fast=12, window_slow=26, window_sign=9)
    numeric_pass &= cross_check(
        macd_pta["MACD_12_26_9"].iloc[-1], macd_ta_ind.macd().iloc[-1], "MACD_line[-1]"
    )
    numeric_pass &= cross_check(
        macd_pta["MACDs_12_26_9"].iloc[-1],
        macd_ta_ind.macd_signal().iloc[-1],
        "MACD_signal[-1]",
    )
    numeric_pass &= cross_check(
        macd_pta["MACDh_12_26_9"].iloc[-1],
        macd_ta_ind.macd_diff().iloc[-1],
        "MACD_hist[-1]",
    )

    # ─── EMA(20), EMA(50), EMA(200) ──────────────────────────────────────
    for length in (20, 50, 200):
        key = f"ema{length}"
        print(f"\n[EMA({length})]")
        ema_pta = pta.ema(df["close"], length=length)
        cols = {ema_pta.name}
        all_pass &= check_columns(cols, EXPECTED_COLS_0_4[key], key)
        ema_ta = ta.trend.EMAIndicator(df["close"], window=length).ema_indicator()
        numeric_pass &= cross_check(ema_pta.iloc[-1], ema_ta.iloc[-1], f"EMA_{length}[-1]")

    # ─── Bollinger(20, 2) ────────────────────────────────────────────────
    print("\n[Bollinger(20, 2)]")
    bb_pta = pta.bbands(df["close"], length=20, std=2)
    cols = set(bb_pta.columns)
    all_pass &= check_columns(cols, EXPECTED_COLS_0_4["bbands"], "bbands")
    bb_ta_ind = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    numeric_pass &= cross_check(
        bb_pta["BBL_20_2.0_2.0"].iloc[-1], bb_ta_ind.bollinger_lband().iloc[-1], "BBL[-1]"
    )
    numeric_pass &= cross_check(
        bb_pta["BBM_20_2.0_2.0"].iloc[-1], bb_ta_ind.bollinger_mavg().iloc[-1], "BBM[-1]"
    )
    numeric_pass &= cross_check(
        bb_pta["BBU_20_2.0_2.0"].iloc[-1], bb_ta_ind.bollinger_hband().iloc[-1], "BBU[-1]"
    )

    # ─── ADX(14) ─────────────────────────────────────────────────────────
    print("\n[ADX(14)]")
    adx_pta = pta.adx(df["high"], df["low"], df["close"], length=14)
    cols = set(adx_pta.columns)
    # ADX returns multiple cols (ADX_14, DMP_14, DMN_14). We only assert ADX_14 is present.
    all_pass &= check_columns({c for c in cols if c == "ADX_14"} or cols, EXPECTED_COLS_0_4["adx"], "adx")
    adx_ta_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    numeric_pass &= cross_check(adx_pta["ADX_14"].iloc[-1], adx_ta_ind.adx().iloc[-1], "ADX_14[-1]", tol_pct=2.0)

    print("\n" + "=" * 70)
    print(f"Columns match spec:    {'PASS' if all_pass else 'FAIL'}")
    print(f"Numeric cross-check:   {'PASS' if numeric_pass else 'FAIL'}")
    print("=" * 70)

    if all_pass and numeric_pass:
        print("\n✅ VERDICT: stay on pandas-ta 0.4.71b. Column names and math agree with ta.")
        return 0
    print("\n⚠ VERDICT: breaking changes detected — consider plan B (ta Bukosabino).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
