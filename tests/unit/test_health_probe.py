"""Unit tests for the source-health probe cache."""

from __future__ import annotations

from typing import ClassVar

import pytest

from mib.services.health_probe import SourceHealthCache
from mib.sources.base import DataSource, RateLimiter


class _StubHealthySource(DataSource):
    name: ClassVar[str] = "stub_ok"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=1, period_seconds=1.0))

    async def health(self) -> bool:
        return True


class _StubDownSource(DataSource):
    name: ClassVar[str] = "stub_down"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=1, period_seconds=1.0))

    async def health(self) -> bool:
        return False


class _StubExplodingSource(DataSource):
    name: ClassVar[str] = "stub_boom"

    def __init__(self) -> None:
        super().__init__(rate_limiter=RateLimiter(max_calls=1, period_seconds=1.0))

    async def health(self) -> bool:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_probe_all_classifies_correctly() -> None:
    cache = SourceHealthCache()
    await cache.probe_all(
        [_StubHealthySource(), _StubDownSource(), _StubExplodingSource()]
    )
    snap = cache.snapshot()
    assert snap["stub_ok"] == "ok"
    assert snap["stub_down"] == "down"
    assert snap["stub_boom"] == "down"  # exception → down, never raise


@pytest.mark.asyncio
async def test_probe_does_not_raise_if_one_explodes() -> None:
    """Exceptions from one source never block the others."""
    cache = SourceHealthCache()
    # Intentionally put the bad one first; the good one must still be probed.
    await cache.probe_all([_StubExplodingSource(), _StubHealthySource()])
    snap = cache.snapshot()
    assert snap["stub_ok"] == "ok"
    assert snap["stub_boom"] == "down"
