"""Heuristic ticker → TradingView exchange mapping.

TradingView's ``tradingview_ta`` widget endpoint requires the exact
exchange code (``NASDAQ`` / ``NYSE`` / ``AMEX`` …) per ticker — there is
no official public "search" endpoint to resolve it automatically.

We curate a small map of the ~60 most queried tickers across NASDAQ,
NYSE and AMEX, plus ADRs (TSM, ASML on NASDAQ; SHOP on NYSE; etc.) and
common ETFs. For anything we don't know we fall back to NASDAQ because
it returns an error instead of wrong data when the symbol isn't listed
there, which is the honest failure mode the service layer can handle
(TV enrichment is soft-fail, see spec §5).
"""

from __future__ import annotations

# ─── NASDAQ (mainly tech and high-growth) ────────────────────────────
_NASDAQ = {
    # Big-tech + major NASDAQ names
    "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO",
    "NFLX", "ADBE", "AMD", "INTC", "CSCO", "PEP", "COST", "QCOM", "TXN",
    "AMAT", "INTU", "PYPL", "SBUX", "MDLZ", "BKNG", "GILD", "REGN", "VRTX",
    # ETFs listed on NASDAQ
    "QQQ", "QQQM", "TLT", "TQQQ", "SQQQ",
    # Popular ADRs on NASDAQ
    "ASML", "MELI", "PDD", "JD", "BIDU", "NTES",
}

# ─── NYSE (incl. most ADRs and blue-chips) ───────────────────────────
_NYSE = {
    # Blue-chips
    "JPM", "BAC", "WFC", "C", "GS", "MS", "V", "MA", "DIS", "KO", "PEP",
    "PG", "JNJ", "PFE", "MRK", "WMT", "HD", "MCD", "CRM", "IBM", "T",
    "VZ", "XOM", "CVX", "BA", "GE", "CAT", "HON", "UNH", "ABT", "AXP",
    # ADRs on NYSE
    "TSM", "SHOP", "BABA", "TM", "HSBC", "UBS", "BP", "SHEL", "SAP",
    "SONY", "NVO", "NVS", "AZN", "GSK", "UL", "DEO", "PTR", "RIO", "BHP",
    # Indexed ETFs on NYSE Arca
    "DIA", "IWM",
}

# ─── AMEX / NYSE Arca (ETFs mostly) ──────────────────────────────────
_AMEX = {
    "SPY", "GLD", "SLV", "USO", "VXX", "UVXY", "SVXY", "EEM", "EFA",
    "VOO", "VTI", "BIL", "HYG", "LQD", "XLF", "XLK", "XLE", "XLV",
    "XLY", "XLI", "XLU", "XLB", "XLRE", "XLC", "XLP",
}

# ─── Yahoo index prefix → TV INDEX exchange ──────────────────────────
_INDEX_MAP = {
    "^GSPC": ("INDEX", "SPX"),       # S&P 500
    "^DJI":  ("INDEX", "DJI"),       # Dow
    "^IXIC": ("INDEX", "IXIC"),      # Nasdaq Composite
    "^NDX":  ("INDEX", "NDX"),       # Nasdaq-100
    "^VIX":  ("INDEX", "VIX"),       # Volatility
    "^TNX":  ("TVC", "US10Y"),       # 10Y Treasury yield
    "^DXY":  ("TVC", "DXY"),         # Dollar index
    "^FVX":  ("TVC", "US05Y"),
    "^FTSE": ("INDEX", "UKX"),
    "^GDAXI": ("XETR", "DAX"),
    "^FCHI": ("INDEX", "CAC"),
    "^IBEX": ("INDEX", "IBEX35"),    # IBEX 35
    "^N225": ("TVC", "NI225"),
}


def resolve_tv_exchange(ticker: str) -> tuple[str, str]:
    """Return ``(exchange, tv_ticker)`` for TradingView's ``TA_Handler``.

    The returned ``tv_ticker`` is the symbol as TradingView expects it
    (stripped of Yahoo-specific prefixes like ``^`` or suffixes ``=X``).

    Rules:
      1. If it's a Yahoo index alias (``^GSPC``, ``^VIX`` …) → use the
         curated ``_INDEX_MAP`` entry.
      2. If uppercase match in any of ``_NASDAQ`` / ``_NYSE`` / ``_AMEX``
         → that exchange.
      3. Otherwise → fall back to NASDAQ (TV returns a clean 404-style
         error if wrong, which the source layer treats as soft-fail).

    Forex/futures (``EURUSD=X``, ``GC=F``) are **not** supported by TV
    with the same ticker; for those we skip enrichment at the service
    layer. This helper is only called for stock/ETF/index kinds.
    """
    t = ticker.strip().upper()
    if t in _INDEX_MAP:
        return _INDEX_MAP[t]
    if t in _NASDAQ:
        return "NASDAQ", t
    if t in _NYSE:
        return "NYSE", t
    if t in _AMEX:
        return "AMEX", t
    # Fallback: NASDAQ tends to work for US tech and returns a clean
    # 404 when the symbol isn't there (caught as soft-fail upstream).
    return "NASDAQ", t


def is_forex_or_futures(ticker: str) -> bool:
    """True for Yahoo suffixes ``=X`` / ``=F`` — TV enrichment skipped."""
    t = ticker.strip().upper()
    return t.endswith(("=X", "=F"))
