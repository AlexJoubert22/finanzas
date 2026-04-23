"""`POST /ask` — natural-language question router.

Flow:
    1. AIService.plan_query() converts the question into a plan JSON.
    2. The router dispatches to MarketService / MacroService / NewsService
       based on ``plan.intent`` and ``plan.tickers``.
    3. AIService.summarise_answer() turns the gathered data into a
       short natural-language answer.

The endpoint is bounded at 30 s total (spec: "respuesta coherente en
<10s" is the target for happy path). Anything heavier should use the
individual endpoints.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from mib.api.dependencies import (
    get_ai_service,
    get_macro_service,
    get_market_service,
    get_news_service,
)
from mib.logger import logger
from mib.models.ai import AskRequest, AskResponse
from mib.services.ai_service import AIService
from mib.services.macro import MacroService
from mib.services.market import MarketService
from mib.services.news import NewsService

router = APIRouter(tags=["ask"])


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    ai: AIService = Depends(get_ai_service),
    market: MarketService = Depends(get_market_service),
    macro: MacroService = Depends(get_macro_service),
    news: NewsService = Depends(get_news_service),
) -> AskResponse:
    """Answer a natural-language market question using IA + data sources."""
    try:
        plan = await ai.plan_query(body.question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("/ask plan failed: {}", exc)
        raise HTTPException(status_code=502, detail="El planner IA no respondió.") from exc

    collected = await _execute_plan(plan, market, macro, news)
    try:
        answer = await asyncio.wait_for(
            ai.summarise_answer(body.question, plan, collected),
            timeout=25.0,
        )
    except TimeoutError:
        answer = (
            "No he podido sintetizar una respuesta en el tiempo disponible. "
            "Consulta los datos estructurados arriba."
        )
    return AskResponse(
        question=body.question,
        plan=plan,
        data=collected,
        answer=answer,
        generated_at=datetime.now(UTC),
    )


async def _execute_plan(
    plan: dict[str, Any],
    market: MarketService,
    macro: MacroService,
    news: NewsService,
) -> dict[str, Any]:
    """Collect whatever the plan says is needed. Always returns a dict."""
    intent = str(plan.get("intent", "other")).lower()
    tickers = [str(t).strip() for t in plan.get("tickers", []) if t]
    include_news = bool(plan.get("include_news"))
    tf = str(plan.get("timeframe") or "1h")

    out: dict[str, Any] = {}

    # Anything mentioning markets → ship the macro snapshot (cheap, cached).
    if intent in ("macro", "other", "compare"):
        try:
            out["macro"] = (await macro.snapshot()).model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.info("/ask: macro collection failed: {}", exc)

    if tickers:
        symbols: dict[str, Any] = {}
        tasks = {
            t: market.get_symbol(t, ohlcv_timeframe=tf, ohlcv_limit=50)
            for t in tickers[:5]  # cap planner output for budget
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for t, r in zip(tasks.keys(), results, strict=True):
            if isinstance(r, Exception):
                symbols[t] = {"error": str(r)}
                continue
            symbols[t] = r.model_dump()
        out["symbols"] = symbols

    if intent == "news" or include_news:
        ticker = tickers[0] if tickers else None
        try:
            if ticker:
                out["news"] = (await news.for_ticker(ticker, limit=5)).model_dump()
            else:
                out["news"] = (await news.market_stream(limit=5)).model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.info("/ask: news collection failed: {}", exc)

    return out
