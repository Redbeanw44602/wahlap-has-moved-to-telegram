"""Process independent users concurrently, preserving each user's conversation order."""

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any

from telegram import Update
from telegram.ext import BaseUpdateProcessor


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int = 32):
        super().__init__(max_concurrent_updates)
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        self._locks.clear()

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        if not isinstance(update, Update) or update.effective_user is None:
            await coroutine
            return
        user_id = update.effective_user.id
        lock, count = self._locks.get(user_id, (asyncio.Lock(), 0))
        self._locks[user_id] = lock, count + 1
        entered = False
        try:
            async with lock:
                entered = True
                await coroutine
        finally:
            if not entered and inspect.iscoroutine(coroutine):
                coroutine.close()
            _, remaining = self._locks[user_id]
            if remaining == 1:
                del self._locks[user_id]
            else:
                self._locks[user_id] = lock, remaining - 1
