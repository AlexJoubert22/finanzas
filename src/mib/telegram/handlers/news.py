"""/news handler — ticker headlines with sentiment."""

from __future__ import annotations

from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from mib.api.dependencies import get_ai_service, get_news_service
from mib.logger import logger
from mib.telegram.formatters import fmt_news_list


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_html(
            "Uso: <code>/news &lt;ticker&gt;</code>\nEj. /news AAPL"
        )
        return

    ticker = context.args[0].strip()
    try:
        ns = get_news_service()
        resp = await ns.for_ticker(ticker, limit=5)
        # Attach sentiment via IA — bounded concurrency.
        ai = get_ai_service()
        enriched = []
        for item in resp.items:
            sentiment, rationale = await ai.news_sentiment(item.headline, item.summary)
            enriched.append(
                item.model_copy(
                    update={"sentiment": sentiment, "sentiment_rationale": rationale}
                )
            )
        resp = resp.model_copy(update={"items": enriched})
    except Exception as exc:  # noqa: BLE001
        logger.warning("/news {} failed: {}", ticker, exc)
        await update.message.reply_html(
            f"⚠️ No pude obtener noticias de <code>{ticker}</code>."
        )
        return

    # Convert datetime → ISO str for the formatter (which tolerates both).
    payload = resp.model_dump()
    for it in payload.get("items", []):
        pub = it.get("published_at")
        if isinstance(pub, datetime):
            it["published_at"] = pub.isoformat()
    body = fmt_news_list(payload)
    await update.message.reply_html(body, disable_web_page_preview=True)
