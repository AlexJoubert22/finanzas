"""FRED (Federal Reserve Economic Data) — US macroeconomic series.

Free, no official rate limit but we cap at 30/min out of politeness.
TTL 6 h per spec §4 because most series update daily/monthly.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

import httpx

from mib.config import get_settings
from mib.logger import logger
from mib.sources.base import DataSource, RateLimiter, SourceError

_BASE_URL = "https://api.stlouisfed.org/fred"
_TTL_SEC = 6 * 3600
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)


# Canonical series we care about for the /macro endpoint.
FRED_SERIES = {
    "10y_yield":      "DGS10",       # 10-Year Treasury Constant Maturity Rate (%)
    "2y_yield":       "DGS2",        # 2-Year Treasury
    "fed_funds":      "DFF",         # Effective Federal Funds Rate
    "cpi_yoy":        "CPIAUCSL",    # CPI (we'll compute YoY downstream)
    "unemployment":   "UNRATE",      # US unemployment rate (%)
    "dxy":            "DTWEXBGS",    # Trade-Weighted USD Index (broad)
}


class FREDSource(DataSource):
    name: ClassVar[str] = "fred"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=30, period_seconds=60.0))
        self._api_key = get_settings().fred_api_key

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self._api_key:
            raise SourceError("fred: FRED_API_KEY not set")
        params = {**params, "api_key": self._api_key, "file_type": "json"}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(f"{_BASE_URL}{path}", params=params)
            r.raise_for_status()
            return r.json()

    # ─── Public API ────────────────────────────────────────────────────

    async def fetch_latest_observation(self, series_id: str) -> dict[str, Any]:
        """Return the most recent non-missing observation for ``series_id``.

        FRED returns ``value="."``  when a datum is pending; we walk backwards
        from the newest observation until we find a real number.
        """
        async def loader() -> dict[str, Any]:
            return cast(
                dict[str, Any],
                await self._get(
                    "/series/observations",
                    params={
                        "series_id": series_id,
                        "sort_order": "desc",
                        "limit": 10,
                    },
                ),
            )

        raw = await self._cached_call(
            cache_key=f"fred:obs:{series_id}",
            ttl_seconds=_TTL_SEC,
            endpoint=f"series/observations:{series_id}",
            loader=loader,
        )
        obs_list = raw.get("observations", [])
        for obs in obs_list:
            v = obs.get("value", ".")
            if v in (".", "", None):
                continue
            try:
                value = float(v)
            except (TypeError, ValueError):
                continue
            return {
                "series_id": series_id,
                "value": value,
                "date": obs["date"],
                "units": raw.get("units", ""),
            }
        raise SourceError(f"fred: no valid observation in last 10 rows for {series_id}")

    async def fetch_macro_snapshot(self) -> dict[str, Any]:
        """Pull the canonical FRED series needed by ``/macro``."""
        import asyncio  # local import to keep module-level imports spec-clean

        tasks = {k: self.fetch_latest_observation(sid) for k, sid in FRED_SERIES.items()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out: dict[str, Any] = {}
        for key, res in zip(tasks.keys(), results, strict=True):
            if isinstance(res, Exception):
                logger.info("fred: {} missing — {}", key, res)
                out[key] = None
                continue
            out[key] = res
        return out

    async def health(self) -> bool:
        if not self._api_key:
            return False
        try:
            await self.fetch_latest_observation("DGS10")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("fred health probe failed: {}", exc)
            return False
