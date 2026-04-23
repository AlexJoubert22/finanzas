"""In-memory liveness cache for data sources.

The ``/health`` endpoint must answer in <100 ms so it can be polled
aggressively by Uptime-Kuma / Docker healthchecks. We don't want each
poll to hammer upstream APIs, so a background probe runs every 5 min
(spec condition 4), stores results in this module-level dict, and the
endpoint just reads it.

Contract:
    - States per source: ``"ok"``, ``"degraded"``, ``"down"``.
    - First tick runs at app startup so the first ``/health`` call
      after boot has data (not ``not_yet_probed``).
    - A probe failure is caught per-source; a misbehaving source never
      takes down the probe for the others.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from mib.logger import logger
from mib.sources.base import DataSource

ProbeState = Literal["ok", "degraded", "down", "not_yet_probed"]

# Degraded if the probe takes longer than this (in seconds).
_DEGRADED_LATENCY_S = 3.0


class SourceHealthCache:
    """Thread-safe (by virtue of asyncio single-thread) source-status cache.

    Instances live for the life of the process; there's exactly one shared
    via ``get_health_cache()`` in ``api.dependencies``.
    """

    def __init__(self) -> None:
        self._status: dict[str, ProbeState] = {}
        self._last_checked: dict[str, float] = {}
        self._last_latency_ms: dict[str, int] = {}

    def snapshot(self) -> dict[str, ProbeState]:
        """Read-only copy of the current map; safe to return as JSON."""
        return dict(self._status)

    async def probe_all(self, sources: list[DataSource]) -> None:
        """Run ``health()`` on every source concurrently, cap each at 5 s."""
        async def _probe_one(src: DataSource) -> None:
            start = time.monotonic()
            try:
                ok = await asyncio.wait_for(src.health(), timeout=5.0)
                elapsed_s = time.monotonic() - start
                self._last_latency_ms[src.name] = int(elapsed_s * 1000)
                if ok and elapsed_s < _DEGRADED_LATENCY_S:
                    self._status[src.name] = "ok"
                elif ok:
                    self._status[src.name] = "degraded"
                else:
                    self._status[src.name] = "down"
            except TimeoutError:
                self._status[src.name] = "down"
                self._last_latency_ms[src.name] = 5000
                logger.info("health-probe: {} timed out at 5s", src.name)
            except Exception as exc:  # noqa: BLE001 - probe failure is logged not raised
                self._status[src.name] = "down"
                logger.warning("health-probe: {} exploded: {}", src.name, exc)
            finally:
                self._last_checked[src.name] = time.monotonic()

        await asyncio.gather(*(_probe_one(s) for s in sources))


# Module-level singleton (same pattern as `api.dependencies`).
_cache: SourceHealthCache | None = None


def get_health_cache() -> SourceHealthCache:
    global _cache  # noqa: PLW0603 - intentional singleton
    if _cache is None:
        _cache = SourceHealthCache()
    return _cache
