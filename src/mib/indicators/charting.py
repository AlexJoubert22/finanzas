"""Candlestick chart PNG generator backed by ``mplfinance``.

Used by the Telegram ``/chart`` command in Fase 5. Returns raw bytes
so the handler can pipe them straight into Telegram's ``send_photo``
without a temporary file.

Matplotlib is not thread-safe; we always run the render inside
``asyncio.to_thread`` so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

import mplfinance as mpf  # type: ignore[import-untyped]
import pandas as pd

# Pre-configured style so every chart looks the same across calls.
_STYLE = mpf.make_mpf_style(
    base_mpf_style="yahoo",
    marketcolors=mpf.make_marketcolors(
        up="#3BBA6F",
        down="#D14343",
        wick={"up": "#3BBA6F", "down": "#D14343"},
        edge={"up": "#3BBA6F", "down": "#D14343"},
        volume={"up": "#3BBA6F", "down": "#D14343"},
    ),
    facecolor="#ffffff",
    gridcolor="#f0f0f0",
    gridstyle=":",
    rc={"font.size": 10},
)


async def render_candles_png(
    df: pd.DataFrame,
    *,
    title: str,
    timeframe: str,
    overlays: list[pd.Series] | None = None,
) -> bytes:
    """Render ``df`` as a candlestick PNG and return its bytes.

    Args:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` (mplfinance's expected casing) and a
            ``DatetimeIndex``. Feed it normalised by the caller.
        title: Title at the top of the chart (``BTC/USDT · 1h`` for
            example).
        timeframe: Short string rendered as a subtitle; purely cosmetic.
        overlays: Optional extra series plotted on the same axes as the
            candles (e.g. EMA-20, EMA-50). Series must share the index.

    Returns:
        PNG bytes.
    """
    addplots = []
    if overlays:
        for s in overlays:
            addplots.append(mpf.make_addplot(s, width=1.0))

    def _render() -> bytes:
        buf = BytesIO()
        mpf.plot(
            df,
            type="candle",
            style=_STYLE,
            volume=True,
            title=f"{title}  ·  {timeframe}",
            ylabel="Price",
            ylabel_lower="Volume",
            figsize=(10, 6),
            addplot=addplots,
            savefig={
                "fname": buf,
                "dpi": 110,
                "format": "png",
                "bbox_inches": "tight",
                "pad_inches": 0.2,
            },
            returnfig=False,
        )
        return buf.getvalue()

    return await asyncio.to_thread(_render)


def candles_dataframe(candles: list[dict[str, float | str]]) -> pd.DataFrame:
    """Normalise a list of our ``Candle`` dicts into mplfinance format."""
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    return df[["Open", "High", "Low", "Close", "Volume"]]


__all__ = ["render_candles_png", "candles_dataframe"]
