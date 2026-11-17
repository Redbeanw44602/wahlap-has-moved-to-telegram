"""Synchronize the Telegram command menu without blocking bot availability."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from functools import partial
from typing import TypeVar

from telegram import Bot, BotCommand, BotCommandScopeAllPrivateChats
from telegram.error import RetryAfter, TelegramError

logger = logging.getLogger(__name__)
T = TypeVar("T")


def retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return max(0.0, value.total_seconds() if isinstance(value, timedelta) else float(value))


async def _sleep(seconds: float) -> None:
    while seconds > 0:
        interval = min(seconds, 60.0)
        await asyncio.sleep(interval)
        seconds -= interval


async def _request(endpoint: str, operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except RetryAfter as exc:
        logger.warning(
            "Telegram %s 限流，%.1f 秒后重试",
            endpoint,
            retry_after_seconds(exc) + 0.5,
        )
        raise


class BotCommandsSync:
    def __init__(self, bot: Bot, commands: Sequence[BotCommand]):
        self.bot = bot
        self.commands = tuple(commands)
        self.scope = BotCommandScopeAllPrivateChats()
        self._tasks: set[asyncio.Task] = set()

    async def _sync_commands(self) -> None:
        current = await _request(
            "getMyCommands", partial(self.bot.get_my_commands, scope=self.scope)
        )
        if tuple(current) != self.commands:
            await _request(
                "setMyCommands",
                partial(self.bot.set_my_commands, self.commands, scope=self.scope),
            )
            logger.info("命令菜单已更新")
        else:
            logger.debug("命令菜单无需更新")

    async def _attempt(self, label: str, operation: Callable[[], Awaitable[None]]) -> float | None:
        try:
            await operation()
        except RetryAfter as exc:
            return retry_after_seconds(exc) + 0.5
        except TelegramError as exc:
            # Error text may contain request URLs or credentials.
            logger.warning("%s同步失败：%s", label, type(exc).__name__)
        return None

    async def _retry(
        self, label: str, operation: Callable[[], Awaitable[None]], delay: float
    ) -> None:
        while True:
            await _sleep(delay)
            next_delay = await self._attempt(label, operation)
            if next_delay is None:
                return
            delay = next_delay

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("命令菜单同步失败：%s", type(error).__name__)

    async def start(self) -> None:
        delay = await self._attempt("命令菜单", self._sync_commands)
        if delay is not None:
            task = asyncio.create_task(
                self._retry("命令菜单", self._sync_commands, delay), name="telegram-commands-retry"
            )
            self._tasks.add(task)
            task.add_done_callback(self._finished)

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
