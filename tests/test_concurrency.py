import asyncio
import inspect
from contextlib import suppress
from datetime import UTC, datetime

from telegram import Chat, Message, Update, User

from wahlap_bot.concurrency import PerUserUpdateProcessor


def update(user_id):
    return Update(
        user_id,
        Message(
            1,
            datetime.now(UTC),
            Chat(user_id, "private"),
            from_user=User(user_id, "Player", False),
        ),
    )


async def test_same_user_is_serial_and_locks_are_released():
    processor = PerUserUpdateProcessor(4)
    order = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def first():
        order.append("first-start")
        started.set()
        await release.wait()
        order.append("first-end")

    async def second():
        order.append("second")

    task1 = asyncio.create_task(processor.process_update(update(101), first()))
    await started.wait()
    task2 = asyncio.create_task(processor.process_update(update(101), second()))
    await asyncio.sleep(0)
    assert order == ["first-start"]
    release.set()
    await asyncio.gather(task1, task2)
    assert order == ["first-start", "first-end", "second"]
    assert processor._locks == {}


async def test_cancelled_waiter_closes_its_coroutine_and_releases_lock():
    processor = PerUserUpdateProcessor(4)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first():
        started.set()
        await release.wait()

    async def waiter():
        raise AssertionError("A cancelled waiter must not run")

    task = asyncio.create_task(processor.process_update(update(101), first()))
    await started.wait()
    coroutine = waiter()
    waiting = asyncio.create_task(processor.process_update(update(101), coroutine))
    await asyncio.sleep(0)
    waiting.cancel()
    with suppress(asyncio.CancelledError):
        await waiting
    release.set()
    await task
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    assert processor._locks == {}
