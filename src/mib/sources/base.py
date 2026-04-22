"""Abstract base for every external data source.

Each concrete source (CCXT, yfinance, CoinGecko, …) subclasses
``DataSource`` and provides two things:

1. A unique ``name`` class variable used for logging, cache scoping, and
   metrics.
2. A ``RateLimiter`` instance tuned to the upstream's free-tier limits.

The ``call`` helper wraps the actual upstream invocation with:

- rate limiting (token bucket with jitter on replenish).
- TTL-based caching via ``CacheStore``.
- latency + success/failure logging into the ``source_calls`` table.
- tenacity retries with exponential back-off for transient errors.

Per spec §12, this module is subject to ``mypy --strict``.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from random import uniform
from typing import Any, ClassVar, TypeVar, cast

from sqlalchemy import insert
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from mib.cache.store import CacheStore
from mib.db.models import SourceCall
from mib.db.session import async_session_factory
from mib.logger import logger

T = TypeVar("T")


class SourceError(Exception):
    """Raised by a DataSource when the upstream fails after retries."""


class RateLimitError(SourceError):
    """Raised when our local rate limiter refuses the call."""


@dataclass
class RateLimiter:
    """Simple async token bucket.

    Not global — each DataSource owns its own instance with limits
    chosen from the free-tier docs of the provider.

    Args:
        max_calls: Upper bound of calls allowed within ``period_seconds``.
        period_seconds: Rolling window size in seconds.
    """

    max_calls: int
    period_seconds: float
    _tokens: float = 0.0
    _last_refill: float = 0.0
    _lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self._tokens = float(self.max_calls)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available.

        Implements the leaky-bucket refill math: tokens regenerate at
        ``max_calls / period_seconds`` per second. Waits with a small
        jitter to avoid thundering-herd when multiple coroutines are
        blocked on the same limiter.
        """
        assert self._lock is not None
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                refill = elapsed * (self.max_calls / self.period_seconds)
                if refill > 0:
                    self._tokens = min(float(self.max_calls), self._tokens + refill)
                    self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Wait the minimum time needed for one token plus jitter.
                needed = (1.0 - self._tokens) * (self.period_seconds / self.max_calls)
                sleep_for = needed + uniform(0.0, needed * 0.1)
                await asyncio.sleep(sleep_for)


class DataSource(ABC):
    """Base class for every external data source."""

    #: Short unique identifier (``"ccxt"``, ``"yfinance"``, …). Used for
    #: cache keys, metrics and logs. Concrete subclasses MUST override.
    name: ClassVar[str] = ""

    #: Retry config for transient errors. Subclasses can tune it.
    max_retries: ClassVar[int] = 3

    def __init__(self, rate_limiter: RateLimiter) -> None:
        """Initialise the source with its own rate limiter."""
        if not self.name:
            msg = f"{type(self).__name__} must set the `name` class attribute"
            raise RuntimeError(msg)
        self._limiter = rate_limiter
        self._cache: CacheStore[Any] = CacheStore(source=self.name)

    @abstractmethod
    async def health(self) -> bool:
        """Return True if the source is currently reachable.

        Implementations should be cheap — ideally a cached last-probe
        answer. Used by the ``/health`` endpoint in later phases.
        """

    # ─── Helpers exposed to subclasses ──────────────────────────────────

    async def _cached_call(
        self,
        cache_key: str,
        ttl_seconds: int,
        endpoint: str,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        """Run ``loader`` with rate limit + cache + retries + metrics.

        Flow:
            1. If the value is already in the cache and fresh → return it.
            2. Otherwise:
                a. Wait for a token from the rate limiter.
                b. Call ``loader`` under the retry policy.
                c. Cache the result under ``cache_key`` for ``ttl_seconds``.
                d. Log a row to ``source_calls`` for metrics.
        """
        hit_value, hit = await self._cache.get_or_set(
            cache_key, ttl_seconds, loader=lambda: self._run_with_retry(endpoint, loader)
        )
        if hit:
            await self._log_source_call(endpoint=endpoint, latency_ms=0, success=True, cached=True)
        # CacheStore stores arbitrary JSON; the caller guarantees T.
        return cast(T, hit_value)

    async def _run_with_retry(
        self,
        endpoint: str,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        """Call ``loader`` with tenacity retries and metrics logging."""
        await self._limiter.acquire()
        started = time.monotonic()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential_jitter(initial=0.5, max=4.0),
                retry=retry_if_exception_type((TimeoutError, ConnectionError)),
                reraise=True,
            ):
                with attempt:
                    value = await loader()
        except RetryError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            await self._log_source_call(
                endpoint=endpoint, latency_ms=elapsed_ms, success=False, error=str(exc)
            )
            raise SourceError(f"{self.name} failed after retries: {exc}") from exc
        except Exception as exc:
            # Non-retriable errors: log and re-raise as SourceError.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            await self._log_source_call(
                endpoint=endpoint, latency_ms=elapsed_ms, success=False, error=str(exc)
            )
            raise SourceError(f"{self.name} non-retriable error: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        await self._log_source_call(
            endpoint=endpoint, latency_ms=elapsed_ms, success=True, cached=False
        )
        return value

    async def _log_source_call(
        self,
        *,
        endpoint: str,
        latency_ms: int,
        success: bool,
        cached: bool = False,
        error: str | None = None,
    ) -> None:
        """Append a row to ``source_calls`` — fire-and-forget via a short session."""
        try:
            async with async_session_factory() as session:
                await session.execute(
                    insert(SourceCall).values(
                        source=self.name,
                        endpoint=endpoint,
                        latency_ms=latency_ms,
                        success=success,
                        cached=cached,
                        error=error,
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover - metrics failure shouldn't break the caller
            logger.warning("source_calls insert failed ({}): {}", self.name, exc)
