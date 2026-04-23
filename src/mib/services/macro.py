"""Macro snapshot service.

Builds the ``MacroResponse`` by fanning out to:
- yfinance for SPX (``^GSPC``) and VIX (``^VIX``) quotes.
- FRED for the 10Y Treasury yield (``DGS10``) and the broad USD index
  (``DTWEXBGS``). Yahoo's ``^DXY`` needs a paid feed, FRED's is free.
- CoinGecko for global crypto dominance (``/global`` → BTC dominance %).

Every fan-out is wrapped in ``asyncio.gather(return_exceptions=True)`` so a
missing key or transient outage for one source never blocks the others.
Missing KPIs get ``value=None`` and the response still serialises cleanly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from mib.logger import logger
from mib.models.macro import MacroKPI, MacroResponse
from mib.sources.coingecko import CoinGeckoSource
from mib.sources.fred import FREDSource
from mib.sources.yfinance_source import YFinanceSource


class MacroService:
    def __init__(
        self,
        yf: YFinanceSource,
        fred: FREDSource,
        cg: CoinGeckoSource,
    ) -> None:
        self._yf = yf
        self._fred = fred
        self._cg = cg

    async def snapshot(self) -> MacroResponse:
        # Fan out.
        spx_t, vix_t, y10_t, dxy_t, cg_t = await asyncio.gather(
            self._yf.fetch_quote("^GSPC"),
            self._yf.fetch_quote("^VIX"),
            self._fred.fetch_latest_observation("DGS10"),
            self._fred.fetch_latest_observation("DTWEXBGS"),
            self._cg.fetch_global(),
            return_exceptions=True,
        )

        spx = _kpi_from_quote("S&P 500", "^GSPC", spx_t, "yfinance")
        vix = _kpi_from_quote("VIX", "^VIX", vix_t, "yfinance")
        yield_10y = _kpi_from_fred("10Y Treasury", "DGS10", y10_t, unit="%")
        dxy = _kpi_from_fred("USD Index (TWEX broad)", "DTWEXBGS", dxy_t, unit="idx")
        btc_dom = _kpi_from_cg_global(cg_t)

        return MacroResponse(
            spx=spx,
            vix=vix,
            dxy=dxy,
            yield_10y=yield_10y,
            btc_dominance=btc_dom,
            generated_at=datetime.now(UTC),
        )


# ─── Adapters: raw source result → MacroKPI ──────────────────────────

def _kpi_from_quote(label: str, ticker: str, raw: object, source: str) -> MacroKPI:
    if isinstance(raw, Exception):
        logger.info("macro: yfinance {} missing: {}", ticker, raw)
        return MacroKPI(label=label, ticker=ticker, source=source)
    from mib.models.market import Quote  # local to avoid cycles

    assert isinstance(raw, Quote)
    return MacroKPI(
        label=label,
        ticker=ticker,
        value=raw.price,
        change_pct=raw.change_24h_pct,
        unit=raw.currency or "",
        source=source,
        as_of=raw.timestamp,
    )


def _kpi_from_fred(label: str, series_id: str, raw: object, *, unit: str) -> MacroKPI:
    if isinstance(raw, Exception):
        logger.info("macro: FRED {} missing: {}", series_id, raw)
        return MacroKPI(label=label, ticker=series_id, source="fred", unit=unit)
    assert isinstance(raw, dict)
    try:
        as_of = datetime.fromisoformat(raw["date"]).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        as_of = None
    return MacroKPI(
        label=label,
        ticker=series_id,
        value=float(raw["value"]),
        unit=unit,
        source="fred",
        as_of=as_of,
    )


def _kpi_from_cg_global(raw: object) -> MacroKPI:
    if isinstance(raw, Exception):
        logger.info("macro: CoinGecko global missing: {}", raw)
        return MacroKPI(label="BTC Dominance", ticker="BTC.D", source="coingecko", unit="%")
    assert isinstance(raw, dict)
    return MacroKPI(
        label="BTC Dominance",
        ticker="BTC.D",
        value=float(raw["btc_dominance_pct"]),
        unit="%",
        source="coingecko",
        as_of=datetime.now(UTC),
    )
