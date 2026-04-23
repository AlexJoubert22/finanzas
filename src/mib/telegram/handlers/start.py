"""/start + /help command handlers."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from mib.telegram.formatters import esc

_WELCOME = """👋 <b>Bienvenido a MIB · Finanzas</b>

Tu asistente para datos de mercado. Comandos disponibles:

<b>Consultas</b>
/price &lt;ticker&gt; — precio + indicadores + análisis IA
/chart &lt;ticker&gt; [tf] — gráfico de velas (tf: 1h, 4h, 1d)
/scan [preset] — scanner (oversold | breakout | trending)
/news &lt;ticker&gt; — últimas 3-5 noticias con sentiment
/macro — SPX, VIX, DXY, 10Y yield, BTC dominance
/ask &lt;pregunta&gt; — pregunta natural sobre mercados

<b>Alertas</b>
/watch &lt;ticker&gt; &lt;op&gt; &lt;precio&gt; — crear alerta (op: &gt; o &lt;)
/alerts — ver alertas activas

<b>Meta</b>
/status — uptime, fuentes, cuotas IA
/help [comando] — ayuda detallada

<i>No es consejo financiero. Consulta a un profesional cualificado.</i>"""


_HELP_DETAIL: dict[str, str] = {
    "price": (
        "<b>/price</b> &lt;ticker&gt;\n"
        "Precio actual + RSI/MACD/EMA/Bollinger/ADX + rating TradingView + "
        "análisis IA.\nEjemplos: <code>/price BTC/USDT</code>, "
        "<code>/price AAPL</code>, <code>/price ^GSPC</code>"
    ),
    "chart": (
        "<b>/chart</b> &lt;ticker&gt; [timeframe]\n"
        "Gráfico de velas PNG. timeframe: 1h (default), 4h, 1d.\n"
        "Ejemplo: <code>/chart ETH/USDT 4h</code>"
    ),
    "scan": (
        "<b>/scan</b> [preset]\n"
        "Screener. Presets: <code>oversold</code> (RSI&lt;30, 1h), "
        "<code>breakout</code> (EMA20 cruza EMA50, 4h), "
        "<code>trending</code> (ADX&gt;25, 1d).\n"
        "Ejemplo: <code>/scan oversold</code>"
    ),
    "news": (
        "<b>/news</b> &lt;ticker&gt;\n"
        "Últimas 3-5 noticias con sentiment bullish/bearish/neutral.\n"
        "Ejemplo: <code>/news AAPL</code>"
    ),
    "macro": "<b>/macro</b>\nSnapshot macro: SPX, VIX, DXY, 10Y yield, BTC dominance.",
    "watch": (
        "<b>/watch</b> &lt;ticker&gt; &lt;op&gt; &lt;precio&gt;\n"
        "Crea una alerta. op: <code>&gt;</code> o <code>&lt;</code>.\n"
        "Ejemplo: <code>/watch BTC/USDT &gt; 100000</code>"
    ),
    "alerts": "<b>/alerts</b>\nLista tus alertas activas con botón de borrar.",
    "ask": (
        "<b>/ask</b> &lt;pregunta&gt;\n"
        "Pregunta en lenguaje natural. Ej: "
        "<code>/ask cómo está el mercado cripto hoy?</code>"
    ),
    "status": "<b>/status</b>\nEstado interno: uptime, fuentes, cuotas IA.",
}


async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_html(_WELCOME, disable_web_page_preview=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if context.args:
        name = context.args[0].lstrip("/").lower()
        if name in _HELP_DETAIL:
            await update.message.reply_html(_HELP_DETAIL[name])
            return
        await update.message.reply_html(
            f"Comando desconocido: <code>{esc(name)}</code>\n"
            "Usa /help sin argumento para ver todos."
        )
        return
    await update.message.reply_html(_WELCOME, disable_web_page_preview=True)
