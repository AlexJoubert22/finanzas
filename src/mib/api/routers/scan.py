"""`GET /scan?preset=…` — rule-based multi-ticker screener."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from mib.api.dependencies import get_ai_service, get_scanner_service
from mib.logger import logger
from mib.models.ai import ScanHit, ScanResponse
from mib.services.ai_service import AIService
from mib.services.scanner import ScannerService, load_scanner_presets

router = APIRouter(tags=["scan"])


_DEFAULT_CRYPTO = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
_DEFAULT_STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN", "SPY", "QQQ"]


@router.get("/scan", response_model=ScanResponse)
async def run_scan(
    preset: Literal["oversold", "breakout", "trending"] = Query(default="oversold"),
    tickers: str | None = Query(
        default=None,
        description="Comma-separated tickers. If empty, use the config default set.",
    ),
    summarise: bool = Query(
        default=False,
        description="If true, wrap hits in a short IA summary (spec §4).",
    ),
    scanner: ScannerService = Depends(get_scanner_service),
    ai: AIService = Depends(get_ai_service),
) -> ScanResponse:
    """Evaluate ``preset`` against a ticker universe and return the hits."""
    universe = _resolve_universe(tickers)
    if not universe:
        raise HTTPException(status_code=400, detail="Lista de tickers vacía.")
    try:
        hits = await scanner.run(preset, universe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("/scan failed: {}", exc)
        raise HTTPException(status_code=502, detail="Error ejecutando el scanner.") from exc

    summary = ""
    if summarise and hits:
        try:
            summary = await ai.scan_summary(preset, hits)
        except Exception as exc:  # noqa: BLE001
            logger.info("/scan summary soft-fail: {}", exc)

    return ScanResponse(
        preset=preset,
        tickers_scanned=len(universe),
        hits=[ScanHit(**h) for h in hits],
        summary=summary,
        generated_at=datetime.now(UTC),
    )


def _resolve_universe(tickers: str | None) -> list[str]:
    if tickers:
        return [t.strip() for t in tickers.split(",") if t.strip()]
    cfg = load_scanner_presets()
    defaults = cfg.get("default_tickers", {}) if cfg else {}
    crypto = defaults.get("crypto") or _DEFAULT_CRYPTO
    stocks = defaults.get("stocks") or _DEFAULT_STOCKS
    return list(crypto) + list(stocks)
