"""Candlestick chart PNG generator backed by ``mplfinance``.

Used by the Telegram ``/chart`` command (fase 5+). Returns raw bytes so
the handler can pipe them straight into Telegram's ``send_photo`` without
a temporary file.

**Lazy imports** (spec FASE 5 pre-polish): ``matplotlib`` + ``mplfinance``
pesan ~35 MiB en RSS cuando se importan en eager mode — y la aplicación
no los necesita en ``/symbol``, ``/macro``, ``/news``, ``/ask`` ni
``/scan``. Los diferimos al primer ``render_candles_png()`` para que el
baseline del container no pague ese coste sin usarlos.

Matplotlib is not thread-safe; we always run the render inside
``asyncio.to_thread`` so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from io import BytesIO
from typing import Any

import pandas as pd

# ─── Lazy mplfinance/matplotlib bootstrap ─────────────────────────────

# matplotlib writes its cache into $HOME (/app in the container, which is
# owned by our non-root user `mib`). We redirect it to /tmp so it does
# not fight ``install -d -o mib ... /app``.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-cache")


@lru_cache(maxsize=1)
def _mpf() -> Any:
    """Import mplfinance once on first chart request."""
    import mplfinance as mpf  # noqa: PLC0415 - intentional lazy import

    return mpf


@lru_cache(maxsize=1)
def _style() -> Any:
    """Build (once) the shared chart style."""
    mpf = _mpf()
    return mpf.make_mpf_style(
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
        title: Title at the top of the chart (``BTC/USDT · 1h`` for example).
        timeframe: Short string rendered as a subtitle; purely cosmetic.
        overlays: Optional extra series plotted on the same axes as the
            candles (e.g. EMA-20, EMA-50). Series must share the index.

    Returns:
        PNG bytes.
    """
    mpf = _mpf()
    style = _style()

    addplots: list[Any] = []
    if overlays:
        addplots = [mpf.make_addplot(s, width=1.0) for s in overlays]

    def _render() -> bytes:
        buf = BytesIO()
        mpf.plot(
            df,
            type="candle",
            style=style,
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
