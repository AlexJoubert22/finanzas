"""``/backtest`` Telegram command (FASE 12.6).

Two operating modes:

- ``/backtest`` (no args)       → list the most recent runs across all
  strategies the operator has stored. Useful for quickly grabbing a
  run id to inspect.
- ``/backtest <run_id>``        → fetch one persisted run and ship its
  metrics summary + the equity-curve PNG inline.

Launching brand-new backtests over Telegram (with a date range +
universe + slippage args) is intentionally NOT in this commit —
running them requires real OHLCV data which the bot fetches via the
HTTP API or a manual orchestration tool. The handler here surfaces
the persisted runs so the operator can review without context-
switching to the dashboard.
"""

from __future__ import annotations

import io

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_backtest_run_repo
from mib.backtest.plotting import render_equity_curve_png
from mib.logger import logger
from mib.telegram.formatters import esc


async def backtest_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None:
        return
    args = context.args or []
    if not args:
        await _list_recent(update)
        return
    try:
        run_id = int(args[0])
    except ValueError:
        await update.message.reply_html(
            "Uso: <code>/backtest [run_id]</code>\n"
            "Sin args: lista los últimos runs persistidos."
        )
        return
    await _ship_run(update, run_id)


async def _list_recent(update: Update) -> None:
    """List the most recent runs across the operator's tracked strategies.

    The repo's ``list_by_strategy`` is per-strategy; we hit the well-
    known preset names so the command is useful without arguments.
    """
    if update.message is None:
        return
    repo = get_backtest_run_repo()
    presets = ("scanner.oversold.v1", "scanner.breakout.v1", "scanner.trending.v1")
    lines = ["📊 <b>Backtest runs (recientes)</b>"]
    total = 0
    for strategy in presets:
        try:
            runs = await repo.list_by_strategy(strategy, limit=3)
        except Exception as exc:  # noqa: BLE001
            logger.debug("/backtest list failed for {}: {}", strategy, exc)
            continue
        if not runs:
            continue
        lines.append(f"\n<b>{esc(strategy)}</b>")
        for r in runs:
            lines.append(
                f"  #{r.id}  "
                f"{esc(r.date_range_start)}…{esc(r.date_range_end)}  "
                f"trades=<code>{r.total_trades}</code>  "
                f"final=<code>{r.final_equity_quote}</code>"
            )
            total += 1
    if total == 0:
        lines.append("\n<i>(sin runs persistidos todavía)</i>")
    lines.append(
        "\nUso: <code>/backtest &lt;run_id&gt;</code> para ver detalle + curva."
    )
    await update.message.reply_html("\n".join(lines))


async def _ship_run(update: Update, run_id: int) -> None:
    if update.message is None:
        return
    repo = get_backtest_run_repo()
    try:
        run = await repo.get_by_id(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("/backtest fetch failed: {}", exc)
        await update.message.reply_html(
            f"❌ <b>/backtest fallo:</b> {esc(str(exc))}"
        )
        return
    if run is None:
        await update.message.reply_html(
            f"⚠️ Run <code>#{run_id}</code> no encontrado."
        )
        return

    metrics = run.metrics or {}
    summary = (
        f"📊 <b>Backtest #{run.id}</b> — <code>{esc(run.strategy_id)}</code>\n"
        f"  rango: <code>{esc(run.date_range_start)}…{esc(run.date_range_end)}</code>\n"
        f"  universo: <code>{esc(', '.join(run.universe))}</code>\n"
        f"  capital inicial: <code>{run.initial_capital_quote}</code>\n"
        f"  equity final: <code>{run.final_equity_quote}</code>\n"
        f"  trades: <code>{run.total_trades}</code>  "
        f"runtime: <code>{run.runtime_seconds}s</code>\n"
        f"  seed: <code>{run.random_seed}</code>  "
        f"engine: <code>{esc(run.engine_version)}</code>\n"
        "\n<b>Métricas</b>\n"
        f"  PF: <code>{esc(str(metrics.get('profit_factor', 'n/a')))}</code>  "
        f"win_rate: <code>{esc(str(metrics.get('win_rate', 'n/a')))}</code>\n"
        f"  Sharpe: <code>{esc(str(metrics.get('sharpe_ratio', 'n/a')))}</code>  "
        f"Sortino: <code>{esc(str(metrics.get('sortino_ratio', 'n/a')))}</code>\n"
        f"  expectancy: <code>{esc(str(metrics.get('expectancy', 'n/a')))}</code>"
    )

    # Render the curve PNG and ship inline.
    curve = _load_curve_for_telegram(run.equity_curve_path)
    png = render_equity_curve_png(
        curve, title=f"{run.strategy_id} — run #{run.id}"
    )
    try:
        await update.message.reply_photo(
            photo=io.BytesIO(png),
            caption=summary,
            parse_mode="HTML",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("/backtest photo send failed: {}", exc)
        await update.message.reply_html(summary)


def _load_curve_for_telegram(path: str | None) -> list:
    """Best-effort loader. Same parser the HTTP endpoint uses; on any
    error returns an empty list so the renderer falls back to placeholder.
    """
    if not path:
        return []
    from mib.api.routers.backtest import _load_curve  # noqa: PLC0415

    return _load_curve(path)
