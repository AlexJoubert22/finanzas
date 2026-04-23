"""Unit tests for FREDSource."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.mark.asyncio
@respx.mock
async def test_fred_latest_observation_skips_dots(fresh_db: None) -> None:  # noqa: ARG001
    from mib.sources.fred import FREDSource

    src = FREDSource()
    src._api_key = "fake-key"  # noqa: SLF001

    respx.get("https://api.stlouisfed.org/fred/series/observations").mock(
        return_value=httpx.Response(
            200,
            json={
                "units": "Percent",
                "observations": [
                    {"date": "2026-04-23", "value": "."},  # pending
                    {"date": "2026-04-22", "value": "4.12"},
                    {"date": "2026-04-21", "value": "4.10"},
                ],
            },
        )
    )

    out = await src.fetch_latest_observation("DGS10")
    assert out["date"] == "2026-04-22"  # skipped the "." row
    assert out["value"] == pytest.approx(4.12)
    assert out["units"] == "Percent"
