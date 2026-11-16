import asyncio
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotName
from telegram.error import NetworkError, RetryAfter
from telegram.warnings import PTBDeprecationWarning

from wahlap_bot.profile import BotCommandsSync, _sleep, retry_after_seconds

NAME = "maimai DX NET"
COMMANDS = (BotCommand("start", "主菜单"), BotCommand("net", "打开 NET"))


@pytest.fixture(autouse=True)
def modern_ptb_time_fields(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")


def telegram_bot(*, name="Old Name", commands=()):
    return SimpleNamespace(
        get_my_name=AsyncMock(return_value=BotName(name)),
        set_my_name=AsyncMock(return_value=True),
        get_my_commands=AsyncMock(return_value=commands),
        set_my_commands=AsyncMock(return_value=True),
    )


async def test_matching_profile_is_read_without_redundant_writes():
    bot = telegram_bot(name=NAME, commands=COMMANDS)
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    bot.get_my_name.assert_not_awaited()
    bot.get_my_commands.assert_awaited_once_with(scope=BotCommandScopeAllPrivateChats())
    bot.set_my_name.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()
    assert not sync._tasks
    await sync.close()


async def test_only_changed_commands_are_written():
    bot = telegram_bot()
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    bot.set_my_commands.assert_awaited_once_with(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    bot.get_my_commands.return_value = COMMANDS
    await sync.start()
    assert bot.set_my_commands.await_count == 1
    bot.set_my_name.assert_not_awaited()
    await sync.close()


@pytest.mark.parametrize("method", ["get_my_commands", "set_my_commands"])
async def test_rate_limited_profile_call_is_retried_without_blocking_startup(
    monkeypatch, method, caplog
):
    bot = telegram_bot()
    operation = getattr(bot, method)
    success = operation.return_value
    operation.side_effect = [RetryAfter(25), success]
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        assert seconds == 25.5
        waiting.set()
        await release.wait()

    monkeypatch.setattr("wahlap_bot.profile._sleep", sleep)
    sync = BotCommandsSync(bot, COMMANDS)
    try:
        await asyncio.wait_for(sync.start(), 1)
        await asyncio.wait_for(waiting.wait(), 1)
        assert sync._tasks
        assert operation.await_count == 1
        assert "25.5" in caplog.text
        endpoint = {
            "get_my_name": "getMyName",
            "set_my_name": "setMyName",
            "get_my_commands": "getMyCommands",
            "set_my_commands": "setMyCommands",
        }[method]
        assert endpoint in caplog.text
        release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(sync._tasks)), 1)
        assert operation.await_count == 2
    finally:
        await sync.close()


async def test_each_new_retry_after_value_is_obeyed(monkeypatch):
    bot = telegram_bot()
    bot.set_my_commands.side_effect = [RetryAfter(2), RetryAfter(7), True]
    sleep = AsyncMock()
    monkeypatch.setattr("wahlap_bot.profile._sleep", sleep)
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    await asyncio.gather(*tuple(sync._tasks))
    assert sleep.await_args_list == [call(2.5), call(7.5)]
    assert bot.set_my_commands.await_count == 3
    await sync.close()


async def test_deferred_retry_rechecks_before_writing(monkeypatch):
    bot = telegram_bot()
    bot.set_my_commands.side_effect = RetryAfter(30)

    async def other_instance_updated_name(_seconds):
        bot.get_my_commands.return_value = COMMANDS

    monkeypatch.setattr("wahlap_bot.profile._sleep", other_instance_updated_name)
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    await asyncio.gather(*tuple(sync._tasks))
    assert bot.set_my_commands.await_count == 1
    await sync.close()


async def test_shutdown_cancels_long_profile_retry_without_waiting(monkeypatch):
    bot = telegram_bot()
    bot.set_my_commands.side_effect = RetryAfter(86400)
    sleeping = asyncio.Event()

    async def sleep(_seconds):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("wahlap_bot.profile._sleep", sleep)
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    await asyncio.wait_for(sleeping.wait(), 1)
    pending = tuple(sync._tasks)
    await asyncio.wait_for(sync.close(), 1)
    assert all(task.cancelled() for task in pending)
    assert not sync._tasks and bot.set_my_commands.await_count == 1
    await sync.close()


async def test_command_menu_failure_does_not_abort_or_leak_errors(caplog):
    bot = telegram_bot()
    bot.get_my_commands.side_effect = NetworkError("private-token-and-url")
    sync = BotCommandsSync(bot, COMMANDS)
    await sync.start()
    bot.set_my_commands.assert_not_awaited()
    assert "NetworkError" in caplog.text
    assert "private-token-and-url" not in caplog.text
    await sync.close()


@pytest.mark.parametrize("timedelta_mode", [False, True])
async def test_retry_after_accepts_integer_and_timedelta_modes(monkeypatch, timedelta_mode):
    monkeypatch.setenv("PTB_TIMEDELTA", "1" if timedelta_mode else "0")
    warnings = nullcontext() if timedelta_mode else pytest.warns(PTBDeprecationWarning)
    with warnings:
        assert retry_after_seconds(RetryAfter(timedelta(seconds=23))) == 23


async def test_long_wait_is_split_into_cancellable_intervals(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("wahlap_bot.profile.asyncio.sleep", sleep)
    await _sleep(121.5)
    assert sleep.await_args_list == [call(60), call(60), call(1.5)]
