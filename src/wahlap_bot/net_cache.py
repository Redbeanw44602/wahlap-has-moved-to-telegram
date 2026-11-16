"""Bounded account-specific asset caching with duplicate request coalescing."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AssetKey:
    user_id: int
    account: str = field(repr=False)
    url: str = field(repr=False)
    variant: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProxyPayload:
    status: int
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    charset: str | None = None


@dataclass(frozen=True, slots=True)
class CachedAsset:
    payload: ProxyPayload
    expires_at: float


class AssetCache:
    def __init__(self, max_bytes: int, ttl: float, *, clock: Callable[[], float] = time.monotonic):
        self.max_bytes = max_bytes
        self.ttl = ttl
        self.size = 0
        self.entries: OrderedDict[AssetKey, CachedAsset] = OrderedDict()
        self._pending: dict[AssetKey, asyncio.Task[CachedAsset]] = {}
        self._retired: set[asyncio.Task] = set()
        self._closed = False
        self._clock = clock

    def _remove(self, key: AssetKey) -> None:
        entry = self.entries.pop(key)
        self.size -= len(entry.payload.body)

    def _completed(self, task: asyncio.Task) -> None:
        self._retired.discard(task)
        if not task.cancelled():
            task.exception()  # A disconnected browser may no longer await this task.

    async def get(
        self,
        key: AssetKey,
        load: Callable[[], Awaitable[ProxyPayload]],
        cacheable: Callable[[ProxyPayload], bool],
    ) -> tuple[CachedAsset, str]:
        if self._closed:
            raise RuntimeError("Asset cache is closed")
        entry = self.entries.get(key)
        if entry is not None:
            if entry.expires_at > self._clock():
                self.entries.move_to_end(key)
                return entry, "HIT"
            self._remove(key)
        task = self._pending.get(key)
        state = "COALESCED" if task is not None else "MISS"
        if task is None:

            async def fetch() -> CachedAsset:
                try:
                    payload = await load()
                    entry = CachedAsset(payload, self._clock() + self.ttl)
                    size = len(payload.body)
                    if (
                        not self._closed
                        and self.ttl > 0
                        and size <= self.max_bytes
                        and self.max_bytes > 0
                        and cacheable(payload)
                    ):
                        for expired, value in list(self.entries.items()):
                            if value.expires_at <= self._clock():
                                self._remove(expired)
                        while self.entries and (
                            self.size + size > self.max_bytes or len(self.entries) >= 2048
                        ):
                            self._remove(next(iter(self.entries)))
                        self.entries[key] = entry
                        self.size += size
                    return entry
                finally:
                    if self._pending.get(key) is asyncio.current_task():
                        self._pending.pop(key)

            task = asyncio.create_task(fetch(), name="net-static-asset")
            self._pending[key] = task
            task.add_done_callback(self._completed)
        return await asyncio.shield(task), state

    def invalidate(self, user_id: int) -> None:
        for key in list(self.entries):
            if key.user_id == user_id:
                self._remove(key)
        for key, task in list(self._pending.items()):
            if key.user_id == user_id:
                self._pending.pop(key)
                self._retired.add(task)
                task.cancel()

    async def close(self) -> None:
        self._closed = True
        tasks = set(self._pending.values()) | self._retired
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._retired.clear()
        self.entries.clear()
        self.size = 0
