"""Unit tests for the RateLimiter token bucket."""

from __future__ import annotations

import asyncio
import time

import pytest

from mib.sources.base import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_first_calls_immediately() -> None:
    rl = RateLimiter(max_calls=3, period_seconds=1.0)
    t0 = time.monotonic()
    await rl.acquire()
    await rl.acquire()
    await rl.acquire()
    elapsed = time.monotonic() - t0
    # First 3 should be essentially free (bucket pre-filled).
    assert elapsed < 0.1, f"initial calls shouldn't wait, took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_rate_limiter_throttles_after_bucket_exhausted() -> None:
    # 2 calls allowed per 0.5 s → each subsequent one waits ~0.25 s.
    rl = RateLimiter(max_calls=2, period_seconds=0.5)
    await rl.acquire()
    await rl.acquire()
    t0 = time.monotonic()
    await rl.acquire()
    elapsed = time.monotonic() - t0
    # Refill rate is 2/0.5 = 4 tokens/s → 1 token in ~0.25 s.
    assert 0.15 < elapsed < 0.4, f"expected ~0.25 s wait, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_rate_limiter_concurrent_acquires_are_serialised() -> None:
    rl = RateLimiter(max_calls=3, period_seconds=0.3)
    # 6 concurrent acquirers — bucket starts at 3, so 3 wait.
    start = time.monotonic()
    await asyncio.gather(*(rl.acquire() for _ in range(6)))
    elapsed = time.monotonic() - start
    # 3 extra tokens need ~0.3 s to regen (3 / 10 tokens-per-second).
    assert 0.2 < elapsed < 0.6, f"expected ~0.3 s serialisation, got {elapsed:.3f}s"
