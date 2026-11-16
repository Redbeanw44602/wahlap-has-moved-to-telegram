import asyncio
from unittest.mock import AsyncMock

import pytest

from wahlap_bot.net_cache import AssetCache, AssetKey, ProxyPayload


def key(path="image.png", user_id=101):
    return AssetKey(user_id, "account-fingerprint", path)


def payload(body=b"image", status=200):
    return ProxyPayload(status, {"content-type": "image/png"}, body)


def cacheable(value):
    return value.status == 200


async def test_cached_hit_does_not_call_upstream_until_expiry():
    now = [100.0]
    cache = AssetCache(1024, 60, clock=lambda: now[0])
    load = AsyncMock(return_value=payload())
    first, state = await cache.get(key(), load, cacheable)
    assert state == "MISS"
    assert (await cache.get(key(), load, cacheable)) == (first, "HIT")
    load.assert_awaited_once()
    now[0] += 61
    assert (await cache.get(key(), load, cacheable))[1] == "MISS"
    assert load.await_count == 2
    await cache.close()


async def test_lru_evicts_by_body_budget_and_keeps_recently_used_entries():
    cache = AssetCache(9, 60)
    load = AsyncMock(return_value=payload(b"123"))
    for name in ("a", "b", "c"):
        await cache.get(key(name), load, cacheable)
    await cache.get(key("a"), load, cacheable)
    await cache.get(key("d"), load, cacheable)
    assert cache.size == 9
    assert list(cache.entries) == [key("c"), key("a"), key("d")]
    cache.invalidate(101)
    assert cache.size == 0 and not cache.entries
    await cache.close()


@pytest.mark.parametrize("mode", ["disabled", "oversized", "error", "ttl_zero"])
async def test_disabled_oversized_and_failed_responses_are_not_saved(mode):
    cache = AssetCache(0 if mode == "disabled" else 2, 0 if mode == "ttl_zero" else 60)
    value = payload(b"123" if mode == "oversized" else b"x", 429 if mode == "error" else 200)
    load = AsyncMock(return_value=value)
    await cache.get(key(), load, cacheable)
    assert (await cache.get(key(), load, cacheable))[1] == "MISS"
    assert load.await_count == 2 and not cache.entries and cache.size == 0
    await cache.close()


async def test_duplicate_loads_share_one_request_and_cancellation_does_not_abort_other_waiters():
    cache = AssetCache(1024, 60)
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch():
        started.set()
        await release.wait()
        return payload()

    load = AsyncMock(side_effect=fetch)
    first = asyncio.create_task(cache.get(key(), load, cacheable))
    await started.wait()
    second = asyncio.create_task(cache.get(key(), load, cacheable))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    value, state = await second
    assert state == "COALESCED" and value.payload.body == b"image"
    load.assert_awaited_once()
    assert (await cache.get(key(), load, cacheable))[1] == "HIT"
    await cache.close()


async def test_failed_fetch_is_not_left_as_a_poisoned_pending_request():
    cache = AssetCache(1024, 60)
    load = AsyncMock(side_effect=[TimeoutError(), payload()])
    with pytest.raises(TimeoutError):
        await cache.get(key(), load, cacheable)
    assert not cache._pending
    assert (await cache.get(key(), load, cacheable))[1] == "MISS"
    assert load.await_count == 2
    await cache.close()


async def test_invalidation_removes_only_owner_and_cancels_owners_inflight_requests():
    cache = AssetCache(1024, 60)
    load = AsyncMock(return_value=payload())
    await cache.get(key(user_id=202), load, cacheable)
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cache.get(key(), blocked, cacheable))
    await started.wait()
    cache.invalidate(101)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert key(user_id=202) in cache.entries
    assert key() not in cache.entries and key() not in cache._pending
    await cache.close()
    assert cache.size == 0 and not cache._retired


async def test_shutdown_cancels_pending_fetches_and_clears_memory():
    cache = AssetCache(1024, 60)
    started = asyncio.Event()

    async def fetch():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cache.get(key(), fetch, cacheable))
    await started.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cache._pending and not cache.entries and cache.size == 0
    await cache.close()
