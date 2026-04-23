"""/scan handler — run a preset against the default ticker universe."""

from __future__ import annotations

from typing import Literal, cast

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_service, get_scanner_service
from mib.logger import logger
from mib.services.scanner import load_scanner_presets
from mib.telegram.formatters import fmt_scan_result

_VALID_PRESETS = {"oversold", "breakout", "trending"}

_DEFAULT_CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
_DEFAULT_STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "SPY", "QQQ"]


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    preset = "oversold"
    if context.args:
        preset = context.args[0].strip().lower()
    if preset not in _VALID_PRESETS:
        await update.message.reply_html(
            f"Preset inválido: <code>{preset}</code>. "
            f"Usa uno de: {', '.join(sorted(_VALID_PRESETS))}."
        )
        return

    # Default universe from config/scanner_presets.yaml (hot-reloadable).
    cfg = load_scanner_presets()
    defaults = cfg.get("default_tickers", {}) if cfg else {}
    tickers: list[str] = list(defaults.get("crypto") or _DEFAULT_CRYPTO)
    tickers += list(defaults.get("stocks") or _DEFAULT_STOCKS)

    try:
        scanner = get_scanner_service()
        hits = await scanner.run(cast(Literal["oversold", "breakout", "trending"], preset), tickers)
        summary = ""
        if hits:
            try:
                summary = await get_ai_service().scan_summary(preset, hits)
            except Exception as exc:  # noqa: BLE001
                logger.info("/scan summary soft-fail: {}", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("/scan {} failed: {}", preset, exc)
        await update.message.reply_html("⚠️ Error ejecutando el scanner.")
        return

    body = fmt_scan_result(
        {
            "preset": preset,
            "tickers_scanned": len(tickers),
            "hits": hits,
            "summary": summary,
        }
    )
    await update.message.reply_html(body, disable_web_page_preview=True)
