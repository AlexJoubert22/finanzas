"""`GET /symbol/{ticker}` — unified quote + OHLCV for crypto and stocks.

The router auto-detects whether ``ticker`` belongs to crypto (routed
to CCXT / Binance) or stocks/ETFs/forex/indices (routed to yfinance)
using the heuristic documented on
:func:`mib.services.market.detect_ticker_kind`.

TradingView-TA is queried as an opt-in enrichment bounded to 3 s; if
it fails or times out the response is served without
``technical_rating`` and the failure is logged but not surfaced.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from mib.api.dependencies import get_ai_service, get_market_service
from mib.logger import logger
from mib.models.market import SymbolResponse
from mib.services.ai_service import AIService
from mib.services.market import MarketService

router = APIRouter(tags=["symbol"])


@router.get("/symbol/{ticker:path}", response_model=SymbolResponse)
async def get_symbol(
    ticker: str,
    timeframe: str = Query(
        default="1h",
        pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1wk)$",
        description="OHLCV interval.",
    ),
    limit: int = Query(default=100, ge=1, le=500, description="Max candles to return."),
    with_ai: bool = Query(
        default=True,
        description="If true, attach an IA analysis paragraph; false keeps the response text-free.",
    ),
    service: MarketService = Depends(get_market_service),
    ai: AIService = Depends(get_ai_service),
) -> SymbolResponse:
    """Return the latest quote and OHLCV bars for ``ticker``.

    Routing heuristic:
      - Starts with ``^``                        → stocks/indices (yfinance).
      - Ends with ``=X`` or ``=F``               → forex/futures (yfinance).
      - Has ``/`` or ``-`` with a crypto quote   → crypto (CCXT/Binance).
      - Otherwise                                → stocks (yfinance).

    Crypto quotes recognised: ``USDT``, ``USDC``, ``BTC``, ``ETH``, ``EUR``, ``USD``.

    Examples:
        ``/symbol/BTC-USDT``    → Binance BTC/USDT
        ``/symbol/AAPL``        → NASDAQ:AAPL via yfinance
        ``/symbol/%5EGSPC``     → S&P500 (URL-encoded ``^GSPC``)
        ``/symbol/EURUSD=X``    → EURUSD spot forex
    """
    try:
        resp = await service.get_symbol(
            ticker, ohlcv_timeframe=timeframe, ohlcv_limit=limit
        )
    except Exception as exc:  # noqa: BLE001 - we want a generic 502 for upstream failures
        logger.warning("GET /symbol/{} failed: {}", ticker, exc)
        raise HTTPException(
            status_code=502,
            detail=f"No se pudieron obtener datos para '{ticker}'. Prueba más tarde.",
        ) from exc

    if with_ai:
        analysis = await ai.symbol_analysis(resp)
        if analysis:
            resp = resp.model_copy(update={"ai_analysis": analysis})
    return resp
