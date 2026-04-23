"""Candlestick chart PNG generator backed by ``mplfinance``.

Used by the Telegram ``/chart`` command. Returns the **path** to a PNG
written to ``/tmp`` so the Telegram handler can ``send_photo`` by path
and unlink the file afterwards — keeping the big PNG out of Python RAM.

Three RAM-safety mitigations applied since inception (FASE 5 plan):

1. **Bounded concurrency** — ``asyncio.Semaphore(2)`` caps the number
   of concurrent renders. A 3rd request waits; if the wait + render
   exceeds the hard timeout, the handler gets ``None`` and tells the
   user "chart temporalmente no disponible".
2. **Hard timeout 5s** per render via ``asyncio.wait_for``. Matplotlib
   doesn't honour cooperative cancellation inside the thread, but we
   stop awaiting so the coroutine returns fast and the user isn't
   blocked. The leaked thread finishes in background and gets GC'd.
3. **``plt.close('all')``** after every successful render to release
   figure handles and matplotlib's internal axes registry. Without this
   the figures accumulate and drag RAM up by ~10 MiB per chart.

**Lazy imports**: ``matplotlib`` + ``mplfinance`` pesan ~35 MiB en RSS
y la mayoría de los endpoints no los necesitan; los diferimos al primer
``render_candles_png()``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from mib.logger import logger

# ─── Lazy mplfinance/matplotlib bootstrap ─────────────────────────────

# matplotlib writes its cache into $HOME (/app in the container, which is
# owned by our non-root user `mib`). We redirect it to /tmp so it does
# not fight ``install -d -o mib ... /app``.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-cache")

# Bounded concurrency — mitigation 1 of FASE 5.
_RENDER_SEM = asyncio.Semaphore(2)

# Hard per-render budget.
_RENDER_TIMEOUT_S = 5.0


@lru_cache(maxsize=1)
def _mpf() -> Any:
    """Import mplfinance once on first chart request."""
    import mplfinance as mpf  # noqa: PLC0415 - intentional lazy import

    return mpf


@lru_cache(maxsize=1)
def _plt() -> Any:
    """Import matplotlib.pyplot once on first chart request."""
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")  # no GUI backend inside the container
    import matplotlib.pyplot as plt  # noqa: PLC0415

    return plt


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
) -> str | None:
    """Render ``df`` as a candlestick PNG on disk and return its path.

    Returns ``None`` if the semaphore slot or the 5 s budget is not met
    — the Telegram handler interprets ``None`` as "chart temporalmente
    no disponible" without surfacing a stack trace.

    Args:
        df: DataFrame with columns ``Open``, ``High``, ``Low``, ``Close``,
            ``Volume`` and a ``DatetimeIndex``.
        title: Title at the top of the chart.
        timeframe: Short subtitle (cosmetic).
        overlays: Optional extra series (EMA-20/50) sharing the index.

    Returns:
        Path to the PNG inside ``/tmp`` (caller must ``os.unlink`` after
        sending), or ``None`` on timeout / shed.
    """
    mpf = _mpf()
    style = _style()
    plt = _plt()

    out_path = Path(f"/tmp/mib-chart-{uuid.uuid4().hex}.png")

    addplots: list[Any] = []
    if overlays:
        addplots = [mpf.make_addplot(s, width=1.0) for s in overlays]

    def _render() -> None:
        try:
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
                    "fname": str(out_path),
                    "dpi": 100,
                    "format": "png",
                    "bbox_inches": "tight",
                    "pad_inches": 0.2,
                },
                returnfig=False,
            )
        finally:
            # Mitigation 3: always release matplotlib figure state so
            # long-running processes don't accumulate axes registries.
            plt.close("all")

    try:
        async with _RENDER_SEM:
            await asyncio.wait_for(asyncio.to_thread(_render), timeout=_RENDER_TIMEOUT_S)
    except TimeoutError:
        logger.info(
            "charting: render timed out >{}s for {} {}",
            _RENDER_TIMEOUT_S,
            title,
            timeframe,
        )
        # Do NOT try to remove the half-written file here — the thread
        # is still writing. The /tmp orphan will be cleaned up by the
        # next container restart; in the meantime it's bounded (one
        # per timeout event).
        return None
    except Exception as exc:  # noqa: BLE001 - charting must never crash handlers
        logger.warning("charting: render failed: {}", exc)
        with contextlib.suppress(FileNotFoundError):
            out_path.unlink()
        return None

    return str(out_path)


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
