"""Unit tests for the SQLite-backed CacheStore."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_cache_get_returns_miss_for_unknown_key(fresh_db: None) -> None:  # noqa: ARG001
    from mib.cache.store import _MISS, CacheStore

    store = CacheStore("unit-test")
    assert await store.get("nope") is _MISS


@pytest.mark.asyncio
async def test_cache_set_then_get_roundtrip(fresh_db: None) -> None:  # noqa: ARG001
    from mib.cache.store import CacheStore

    store = CacheStore("unit-test")
    await store.set("k", {"hello": "world", "n": 42}, ttl_seconds=60)
    assert await store.get("k") == {"hello": "world", "n": 42}


@pytest.mark.asyncio
async def test_cache_expires_and_is_lazily_dropped(fresh_db: None) -> None:  # noqa: ARG001
    from mib.cache.store import _MISS, CacheStore

    store = CacheStore("unit-test")
    await store.set("short-lived", [1, 2, 3], ttl_seconds=1)
    assert await store.get("short-lived") == [1, 2, 3]
    await asyncio.sleep(1.1)
    assert await store.get("short-lived") is _MISS


@pytest.mark.asyncio
async def test_get_or_set_calls_loader_once_then_hits_cache(fresh_db: None) -> None:  # noqa: ARG001
    from mib.cache.store import CacheStore

    store = CacheStore("unit-test")
    loader_calls = 0

    async def loader() -> dict[str, int]:
        nonlocal loader_calls
        loader_calls += 1
        return {"computed": loader_calls}

    v1, hit1 = await store.get_or_set("key", ttl_seconds=30, loader=loader)
    v2, hit2 = await store.get_or_set("key", ttl_seconds=30, loader=loader)
    assert v1 == {"computed": 1}
    assert v2 == {"computed": 1}  # same value, loader ran once
    assert hit1 is False
    assert hit2 is True
    assert loader_calls == 1
