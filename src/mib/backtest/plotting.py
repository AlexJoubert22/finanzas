"""Equity-curve PNG renderer (FASE 12.6).

Uses matplotlib with the Agg backend so it works headless on a server
without a display. The function returns raw PNG bytes; the FastAPI
endpoint streams them with ``Content-Type: image/png`` and the
Telegram handler ships them as inline photos.

Light theme + 1200×600 + 100 dpi keeps file size <100 KB for a year
of bar-resolution curves and renders cleanly on both desktop and
mobile Telegram clients.
"""

from __future__ import annotations

import io

# Force Agg before any pyplot import — necessary because Telegram /
# FastAPI workers run without a GUI.
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from mib.backtest.equity import EquityPoint  # noqa: E402


def render_equity_curve_png(
    curve: list[EquityPoint],
    *,
    title: str = "Backtest equity curve",
    width: int = 1200,
    height: int = 600,
    dpi: int = 100,
) -> bytes:
    """Render the equity curve to a PNG. Returns raw bytes.

    Empty / single-point curves produce a placeholder figure so the
    endpoint never returns an empty body — easier for the operator to
    diagnose than a 500.
    """
    fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)
    if not curve:
        ax.text(
            0.5, 0.5, "(empty curve)",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_axis_off()
    else:
        # matplotlib stubs are version-sensitive on datetime args;
        # ``Any`` cast keeps mypy strict happy without losing runtime
        # behaviour (matplotlib accepts datetimes natively).
        from typing import Any, cast  # noqa: PLC0415

        ts_any = cast(Any, [p.timestamp for p in curve])
        with_fees = [float(p.equity_with_fees) for p in curve]
        without_fees = [float(p.equity_without_fees) for p in curve]
        ax.plot(ts_any, with_fees, label="With fees", linewidth=1.5)
        ax.plot(
            ts_any, without_fees,
            label="No fees", linestyle="--", alpha=0.6, linewidth=1.0,
        )
        ax.set_xlabel("Time")
        ax.set_ylabel("Equity (quote)")
        ax.set_title(title)
        ax.legend(loc="best")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
