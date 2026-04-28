"""HTML formatters and chunking for Telegram messages.

Spec §6 rules respected:
- ``parse_mode='HTML'`` (not MarkdownV2 — robust against user input).
- Emojis: 🟢 (up) 🔴 (down) ⚪ (neutral) ⚠️ (alert) 📊 (data) 📰 (news).
- Never exceed 4000 chars per message (Telegram limit is 4096; we leave
  margin for HTML entity expansion).
- All user-visible text in Spanish.

Every public ``fmt_*`` function returns a single HTML string (or a list
when the payload is too long and needs chunking).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

# Hard cap per individual message. Telegram allows 4096 raw, but HTML
# entities (`&amp;` vs `&`, etc.) can inflate by ~5 % in the worst case.
_MAX_CHARS = 4000


# ─── Emoji helpers ────────────────────────────────────────────────────


def direction_emoji(change_pct: float | None) -> str:
    """🟢 up, 🔴 down, ⚪ neutral/unknown."""
    if change_pct is None:
        return "⚪"
    if change_pct > 0.1:
        return "🟢"
    if change_pct < -0.1:
        return "🔴"
    return "⚪"


def sentiment_emoji(sentiment: str | None) -> str:
    if sentiment == "bullish":
        return "🟢"
    if sentiment == "bearish":
        return "🔴"
    return "⚪"


# ─── Primitive helpers ────────────────────────────────────────────────


def esc(text: object) -> str:
    """HTML-escape anything user-facing before concatenating into a message."""
    return html.escape(str(text), quote=False)


def chunk(text: str) -> list[str]:
    """Split ``text`` into messages of at most ``_MAX_CHARS`` each.

    Splits at paragraph breaks (``\\n\\n``) when possible so the chunks
    remain readable. Falls back to line breaks, then to hard cuts.
    """
    if len(text) <= _MAX_CHARS:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > _MAX_CHARS:
        # Look for the last paragraph break before the limit.
        cut = remaining.rfind("\n\n", 0, _MAX_CHARS)
        if cut < 0:
            cut = remaining.rfind("\n", 0, _MAX_CHARS)
        if cut < 0:
            cut = _MAX_CHARS
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def fmt_price(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "—"
    # Use comma as thousands sep for readability in ES.
    return f"{value:,.{decimals}f}".replace(",", " ")


def fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    return f"{pct:+.2f}%"


def fmt_ts_utc(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M UTC")


# ─── Full-command formatters ──────────────────────────────────────────


def fmt_price_card(payload: dict[str, Any]) -> str:
    """Render ``/price`` output — 20 lines of quote + indicators + rating + AI."""
    quote = payload.get("quote") or {}
    ind = payload.get("indicators") or {}
    rating = payload.get("technical_rating") or {}
    ai_analysis = payload.get("ai_analysis") or ""

    ticker = esc(quote.get("ticker", "?"))
    price = fmt_price(quote.get("price"), decimals=2)
    currency = esc(quote.get("currency", ""))
    venue = esc(quote.get("venue", "—"))
    change = quote.get("change_24h_pct")
    change_txt = fmt_pct(change)
    emoji = direction_emoji(change)

    lines = [
        f"📊 <b>{ticker}</b> · {venue}",
        f"💰 {price} {currency} <b>{emoji} {change_txt}</b> (24h)",
        "",
    ]

    if ind:
        lines.append("<b>Indicadores</b>")
        if ind.get("rsi_14") is not None:
            rsi = ind["rsi_14"]
            zone = "Sobreventa" if rsi < 30 else "Sobrecompra" if rsi > 70 else "Neutral"
            lines.append(f"RSI(14): {rsi:.1f} · {zone}")
        if ind.get("macd") is not None and ind.get("macd_signal") is not None:
            hist = ind.get("macd_hist") or 0.0
            lines.append(
                f"MACD: {ind['macd']:+.2f}  signal: {ind['macd_signal']:+.2f}  hist: {hist:+.2f}"
            )
        if any(ind.get(k) is not None for k in ("ema_20", "ema_50", "ema_200")):
            emas = " / ".join(
                fmt_price(ind.get(k), decimals=0)
                for k in ("ema_20", "ema_50", "ema_200")
            )
            lines.append(f"EMA 20/50/200: {emas}")
        if ind.get("adx_14") is not None:
            adx = ind["adx_14"]
            strength = "tendencia fuerte" if adx > 25 else "sin tendencia clara"
            lines.append(f"ADX(14): {adx:.1f} · {strength}")
        lines.append("")

    if rating.get("recommendation"):
        rec = esc(rating["recommendation"])
        buy = rating.get("buy", 0)
        sell = rating.get("sell", 0)
        neut = rating.get("neutral", 0)
        lines.append(f"<b>TradingView:</b> {rec}  (🟢 {buy} / ⚪ {neut} / 🔴 {sell})")
        lines.append("")

    if ai_analysis:
        lines.append("<b>Análisis IA:</b>")
        lines.append(esc(ai_analysis))
        lines.append("")

    ts = payload.get("quote", {}).get("timestamp")
    if isinstance(ts, str):
        try:
            ts_obj = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            ts_obj = None
    else:
        ts_obj = ts
    lines.append(
        f"<i>Datos: {venue} · {fmt_ts_utc(ts_obj)} · No es consejo financiero</i>"
    )
    return "\n".join(lines)


def fmt_macro_card(payload: dict[str, Any]) -> str:
    """Render ``/macro`` output — 5 KPIs in a compact block."""
    lines = ["📊 <b>Macro snapshot</b>", ""]
    for key, label in (
        ("spx", "S&P 500"),
        ("vix", "VIX"),
        ("dxy", "USD Index"),
        ("yield_10y", "10Y Treasury"),
        ("btc_dominance", "BTC Dominance"),
    ):
        kpi = payload.get(key) or {}
        val = kpi.get("value")
        cp = kpi.get("change_pct")
        unit = esc(kpi.get("unit") or "")
        emoji = direction_emoji(cp) if cp is not None else "⚪"
        val_s = fmt_price(val, decimals=2)
        cp_s = f" {emoji} {fmt_pct(cp)}" if cp is not None else ""
        lines.append(f"<b>{esc(label)}</b>: {val_s} {unit}{cp_s}")
    lines.append("")
    lines.append("<i>Fuentes: yfinance, FRED, CoinGecko · No es consejo financiero</i>")
    return "\n".join(lines)


def fmt_news_list(payload: dict[str, Any]) -> str:
    """Render ``/news <t>`` output — up to 3 headlines with sentiment."""
    items = payload.get("items") or []
    ticker = esc(payload.get("ticker") or "Mercado")
    lines = [f"📰 <b>Noticias · {ticker}</b>", ""]
    if not items:
        lines.append("<i>Sin noticias recientes.</i>")
    for it in items[:5]:
        emoji = sentiment_emoji(it.get("sentiment"))
        headline = esc((it.get("headline") or "").strip())
        src = esc(it.get("source") or "")
        url = it.get("url")
        if url:
            headline = f'<a href="{esc(url)}">{headline}</a>'
        lines.append(f"{emoji} {headline}")
        lines.append(f"<i>  · {src}</i>")
        rationale = it.get("sentiment_rationale")
        if rationale:
            lines.append(f"<i>  · {esc(rationale)}</i>")
        lines.append("")
    lines.append("<i>No es consejo financiero</i>")
    return "\n".join(lines).rstrip()


def fmt_scan_result(payload: dict[str, Any]) -> str:
    """Render ``/scan`` output — hits + optional IA summary."""
    preset = esc(payload.get("preset", "?"))
    scanned = payload.get("tickers_scanned", 0)
    hits = payload.get("hits") or []
    summary = payload.get("summary") or ""

    lines = [f"🔎 <b>Scanner · preset={preset}</b>", f"<i>Evaluados: {scanned}</i>", ""]
    if not hits:
        lines.append("<i>Sin coincidencias para este preset ahora mismo.</i>")
    else:
        for h in hits[:15]:
            ticker = esc(h.get("ticker", "?"))
            reason = esc(h.get("reason", ""))
            lines.append(f"• <b>{ticker}</b> — {reason}")
    if summary:
        lines.append("")
        lines.append("<b>Resumen IA:</b>")
        lines.append(esc(summary))
    lines.append("")
    lines.append("<i>No es consejo financiero</i>")
    return "\n".join(lines)


def fmt_alerts_list(alerts: list[dict[str, Any]]) -> str:
    """Render ``/alerts`` — list of active price alerts."""
    if not alerts:
        return "⚠️ <b>Sin alertas activas.</b>\nCrea una con <code>/watch TICKER op precio</code>"
    lines = ["⚠️ <b>Alertas activas</b>", ""]
    for a in alerts:
        tkr = esc(a["ticker"])
        op = esc(a["operator"])
        tgt = fmt_price(a["target_price"], decimals=2)
        lines.append(f"• #{a['id']}  <b>{tkr}</b>  {op}  {tgt}")
    return "\n".join(lines)


def fmt_watch_created(ticker: str, op: str, target: float) -> str:
    return (
        f"⚠️ <b>Alerta creada</b>\n"
        f"Te avisaré cuando <b>{esc(ticker)}</b> {esc(op)} {fmt_price(target, decimals=2)}."
    )


def fmt_watch_triggered(
    ticker: str, op: str, target: float, current: float
) -> str:
    emoji = "🟢" if op == ">" else "🔴"
    return (
        f"{emoji} <b>Alerta disparada</b>\n"
        f"<b>{esc(ticker)}</b>: {fmt_price(current, decimals=2)}\n"
        f"condición: {esc(op)} {fmt_price(target, decimals=2)}"
    )


def fmt_ask_answer(question: str, answer: str) -> str:
    return (
        f"🤔 <b>Pregunta:</b> {esc(question)}\n\n"
        f"{esc(answer)}\n\n"
        "<i>No es consejo financiero</i>"
    )


def fmt_status(payload: dict[str, Any]) -> str:
    """Render ``/status`` — uptime + sources + quotas."""
    status = esc(payload.get("status", "?"))
    uptime = int(payload.get("uptime_seconds", 0))
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    lines = [
        f"📊 <b>Estado MIB</b> · <i>{status}</i>",
        f"Uptime: {hours}h {minutes}m",
        "",
        "<b>Fuentes</b>",
    ]
    for src, st in (payload.get("sources_status") or {}).items():
        emoji = "🟢" if st == "ok" else "🔴" if st == "down" else "⚪"
        lines.append(f"  {emoji} {esc(src)} · {esc(st)}")

    quotas = payload.get("ai_quotas") or {}
    if quotas:
        lines.append("")
        lines.append("<b>IA · uso diario</b>")
        for provider, frac in quotas.items():
            pct = f"{float(frac) * 100:.1f}%"
            lines.append(f"  {esc(provider)}: {pct}")
    return "\n".join(lines)


def fmt_signal_card(persisted: Any, *, include_id: bool = True) -> str:
    """Render a :class:`PersistedSignal` as a Telegram HTML card.

    Layout: side + strategy id, entry zone, stop, both targets with
    their R-multiple and implied % move, rationale, ``#id`` footer.
    """
    sig = persisted.signal
    low, high = sig.entry_zone
    entry_mid = (low + high) / 2.0
    stop_pct = (sig.invalidation - entry_mid) / entry_mid * 100.0
    t1_pct = (sig.target_1 - entry_mid) / entry_mid * 100.0
    risk = abs(entry_mid - sig.invalidation)
    side_emoji = {"long": "🟢", "short": "🔴", "flat": "⚪"}.get(sig.side, "⚪")
    lines = [
        f"{side_emoji} <b>{esc(sig.strategy_id)}</b> · <code>{esc(sig.ticker)}</code>",
        f"Side: {esc(sig.side)} · Strength: {sig.strength:.2f}",
        "",
        f"Entry: <code>{fmt_price(low)}</code> – <code>{fmt_price(high)}</code>",
        f"Stop: <code>{fmt_price(sig.invalidation)}</code> ({stop_pct:+.2f}%)",
        f"T1 (1R): <code>{fmt_price(sig.target_1)}</code> ({t1_pct:+.2f}%)",
    ]
    if sig.target_2 is not None and risk > 0:
        t2_pct = (sig.target_2 - entry_mid) / entry_mid * 100.0
        r2 = abs(sig.target_2 - entry_mid) / risk
        lines.append(
            f"T2 ({r2:.1f}R): <code>{fmt_price(sig.target_2)}</code> ({t2_pct:+.2f}%)"
        )
    if sig.rationale:
        lines.append("")
        lines.append(esc(sig.rationale))
    footer = [f"Status: {esc(persisted.status)}"]
    if include_id:
        footer.append(f"#{persisted.id}")
    lines.append("")
    lines.append(" · ".join(footer))
    return "\n".join(lines)


def fmt_pending_signals_list(persisted_signals: list[Any]) -> str:
    """Compact one-line-per-signal summary for ``/signals pending``."""
    if not persisted_signals:
        return "No hay signals pendientes."
    lines = [f"<b>Signals pendientes ({len(persisted_signals)})</b>", ""]
    for p in persisted_signals:
        s = p.signal
        side_emoji = {"long": "🟢", "short": "🔴", "flat": "⚪"}.get(s.side, "⚪")
        lines.append(
            f"#{p.id} {side_emoji} <code>{esc(s.ticker)}</code> · "
            f"{esc(s.strategy_id)} · {fmt_price(s.entry_zone[0])}"
        )
    return "\n".join(lines)


__all__ = [
    "chunk",
    "direction_emoji",
    "esc",
    "fmt_alerts_list",
    "fmt_ask_answer",
    "fmt_macro_card",
    "fmt_news_list",
    "fmt_pct",
    "fmt_pending_signals_list",
    "fmt_price",
    "fmt_price_card",
    "fmt_scan_result",
    "fmt_signal_card",
    "fmt_status",
    "fmt_ts_utc",
    "fmt_watch_created",
    "fmt_watch_triggered",
    "sentiment_emoji",
]
