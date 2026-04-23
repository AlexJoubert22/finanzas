"""High-level IA orchestration on top of the AIRouter.

Each method here is wired to an HTTP endpoint (or an enrichment inside
an existing endpoint):

- :meth:`symbol_analysis` — 2-3 sentence analysis for ``/symbol/{ticker}``.
- :meth:`news_sentiment` — bullish/bearish/neutral classifier for /news.
- :meth:`answer_natural_query` — planner+executor for ``/ask``.
- :meth:`summarise_scan` — 1-2 sentences per ticker for ``/scan``.

Every method is soft-fail: if the router returns ``success=False`` the
caller gets a graceful fallback (no analysis / neutral sentiment /
error string in /ask) rather than an exception.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from mib.ai.models import TaskType
from mib.ai.prompts import (
    SYSTEM_MARKET_ANALYST,
    SYSTEM_NEWS_SENTIMENT,
    SYSTEM_QUERY_ROUTER,
    SYSTEM_SCAN_SUMMARY,
    SYSTEM_SUMMARIZER,
)
from mib.ai.providers.base import AITask
from mib.ai.router import AIRouter
from mib.logger import logger
from mib.models.market import Quote, SymbolResponse, TechnicalSnapshot

Sentiment = Literal["bullish", "bearish", "neutral"]


class AIService:
    def __init__(self, router: AIRouter) -> None:
        self._router = router

    # ─── /symbol enrichment ────────────────────────────────────────────

    async def symbol_analysis(self, resp: SymbolResponse) -> str:
        """One short paragraph of technical analysis for the ticker."""
        prompt = _build_symbol_prompt(resp.quote, resp.indicators)
        task = AITask(
            prompt=prompt,
            system=SYSTEM_MARKET_ANALYST,
            task_type=TaskType.ANALYSIS,
            max_tokens=300,
            temperature=0.3,
        )
        result = await self._router.complete(task)
        if not result.success:
            logger.info("ai_service: symbol_analysis soft-fail: {}", result.error)
            return ""
        return result.content

    # ─── /news enrichment ─────────────────────────────────────────────

    async def news_sentiment(self, headline: str, summary: str | None = None) -> tuple[Sentiment, str]:
        """Classify a headline as bullish/bearish/neutral + short rationale."""
        body = headline if not summary else f"{headline}\n{summary}"
        task = AITask(
            prompt=f"Titular + resumen:\n{body}\n\nDevuelve el JSON solicitado.",
            system=SYSTEM_NEWS_SENTIMENT,
            task_type=TaskType.FAST_CLASSIFY,
            max_tokens=120,
            temperature=0.1,
        )
        result = await self._router.complete(task)
        if not result.success:
            return "neutral", ""
        parsed = _extract_json(result.content) or {}
        sentiment_raw = str(parsed.get("sentiment", "")).lower()
        if sentiment_raw not in ("bullish", "bearish", "neutral"):
            return "neutral", parsed.get("rationale", "")
        return sentiment_raw, str(parsed.get("rationale", ""))[:200]  # type: ignore[return-value]

    # ─── /ask endpoint ────────────────────────────────────────────────

    async def plan_query(self, question: str) -> dict[str, Any]:
        """Turn a natural-language question into a structured plan JSON."""
        task = AITask(
            prompt=f"Pregunta del usuario: «{question}»\n\nDevuelve el JSON solicitado.",
            system=SYSTEM_QUERY_ROUTER,
            task_type=TaskType.REASONING,
            max_tokens=300,
            temperature=0.1,
        )
        result = await self._router.complete(task)
        if not result.success:
            return {"intent": "other", "tickers": [], "error": result.error}
        return _extract_json(result.content) or {
            "intent": "other",
            "tickers": [],
            "error": "could not parse planner output",
        }

    async def summarise_answer(
        self,
        question: str,
        plan: dict[str, Any],
        collected: dict[str, Any],
    ) -> str:
        """Final summariser: question + plan + collected data → short answer."""
        payload = json.dumps(
            {"question": question, "plan": plan, "data": collected},
            default=str,
            ensure_ascii=False,
        )[:6000]  # keep the prompt bounded
        task = AITask(
            prompt=(
                "Con estos datos estructurados responde la pregunta del "
                "usuario en español, en 3-5 frases, citando solo los "
                "datos provistos. No inventes.\n\n"
                f"{payload}"
            ),
            system=SYSTEM_SUMMARIZER,
            task_type=TaskType.SUMMARY,
            max_tokens=400,
            temperature=0.3,
        )
        result = await self._router.complete(task)
        if not result.success:
            return (
                "No he podido generar una respuesta con IA en este momento. "
                "Consulta los datos en los endpoints individuales."
            )
        return result.content

    # ─── /scan summary ────────────────────────────────────────────────

    async def scan_summary(self, preset: str, hits: list[dict[str, Any]]) -> str:
        """One paragraph summarising scanner hits."""
        if not hits:
            return ""
        payload = json.dumps(hits[:20], default=str, ensure_ascii=False)
        task = AITask(
            prompt=(
                f"Preset del scanner: {preset}\n"
                f"Tickers con sus indicadores (lista JSON):\n{payload}"
            ),
            system=SYSTEM_SCAN_SUMMARY,
            task_type=TaskType.SUMMARY,
            max_tokens=400,
            temperature=0.3,
        )
        result = await self._router.complete(task)
        if not result.success:
            return ""
        return result.content


# ─── Helpers ─────────────────────────────────────────────────────────

def _build_symbol_prompt(quote: Quote, ind: TechnicalSnapshot | None) -> str:
    lines = [
        f"Ticker: {quote.ticker}  (kind={quote.kind})",
        f"Precio: {quote.price} {quote.currency or ''}",
    ]
    if quote.change_24h_pct is not None:
        lines.append(f"Cambio 24h: {quote.change_24h_pct:+.2f}%")
    if ind is not None:
        lines.append("Indicadores:")
        if ind.rsi_14 is not None:
            lines.append(f"  RSI(14) = {ind.rsi_14:.2f}")
        if ind.macd is not None:
            lines.append(
                f"  MACD(12,26,9): line={ind.macd:+.2f} signal={ind.macd_signal:+.2f} "
                f"hist={ind.macd_hist:+.2f}"
            )
        for name, val in (("EMA20", ind.ema_20), ("EMA50", ind.ema_50), ("EMA200", ind.ema_200)):
            if val is not None:
                lines.append(f"  {name}: {val:.2f}")
        if ind.bb_lower is not None and ind.bb_middle and ind.bb_upper:
            lines.append(
                f"  Bollinger(20,2): low={ind.bb_lower:.2f}  mid={ind.bb_middle:.2f}  "
                f"up={ind.bb_upper:.2f}"
            )
        if ind.adx_14 is not None:
            lines.append(f"  ADX(14): {ind.adx_14:.2f}")
    lines.append("")
    lines.append("Haz un análisis corto (máx 120 palabras) en español.")
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first balanced JSON object out of the LLM output."""
    if not text:
        return None
    # Strip markdown fences if any.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    m = _JSON_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return None
