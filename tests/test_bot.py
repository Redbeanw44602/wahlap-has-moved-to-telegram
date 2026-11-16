import asyncio
import itertools
import json
import time
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import zxingcpp
from PIL import Image
from telegram.ext import ExtBot

from wahlap_bot.bot import BOT_COMMANDS, create_application
from wahlap_bot.config import Settings
from wahlap_bot.endpoints import UserEndpoint
from wahlap_bot.maiserver import MaiserverError, PlayerQRCode
from wahlap_bot.profile import BotCommandsSync
from wahlap_bot.tunnel import CloudflaredError
from wahlap_bot.webapp import NetGateway


class TestGatewayLifecycle:
    @pytest.fixture
    def bot_settings(self, tmp_path, monkeypatch):
        # Only the socket listener is replaced; the bot owns the real gateway,
        # HTTP client and database lifecycle.
        monkeypatch.setattr(NetGateway, "start", AsyncMock())
        return Settings(
            bot_token="123456:TEST_TOKEN",
            database_path=tmp_path / "gateway.sqlite3",
            webapp_public_url="https://bot.example.com/net/",
        )

    async def test_bot_starts_and_closes_gateway_with_its_http_client(self, bot):
        gateway = bot.handlers.gateway
        assert isinstance(gateway, NetGateway)
        NetGateway.start.assert_awaited_once()
        assert gateway.store is bot.handlers.store
        assert gateway.client is bot.handlers.client
        runner = SimpleNamespace(cleanup=AsyncMock())
        gateway.runner = runner
        await bot.app.post_shutdown(bot.app)
        runner.cleanup.assert_awaited_once()
        assert gateway.runner is None
        assert bot.handlers.client.session.closed

    @pytest.mark.parametrize("command", ["/token", "/reset"])
    async def test_saving_or_resetting_account_revokes_only_owners_sessions(self, bot, command):
        await configure(bot, 101, "https://one.example", "first")
        await configure(bot, 202, "https://two.example", "second")
        sessions = bot.handlers.gateway.sessions
        first_sid, _ = sessions.create(await bot.handlers.store.get(101))
        second_sid, _ = sessions.create(await bot.handlers.store.get(202))
        await bot.send(101, command)
        if command == "/token":
            # Even saving the same credentials must revoke the old session.
            await bot.send(101, "first")
        assert first_sid not in sessions.sessions
        assert second_sid in sessions.sessions


class TestCloudflaredLifecycle:
    @pytest.fixture(params=["quick", "named"])
    def bot_settings(self, tmp_path, monkeypatch, request):
        self.events = []
        self.public_url = (
            "https://random.trycloudflare.com/net/"
            if request.param == "quick"
            else "https://bot.example.com/app/net/"
        )
        self.tunnel = SimpleNamespace(public_url=None, running=False)

        async def start_gateway(_gateway):
            self.events.append("gateway_start")

        async def start_tunnel():
            self.events.append("tunnel_start")
            self.tunnel.public_url = self.public_url
            self.tunnel.running = True
            return self.public_url

        async def close_tunnel():
            self.events.append("tunnel_close")
            self.tunnel.running = False

        self.tunnel.start = AsyncMock(side_effect=start_tunnel)
        self.tunnel.close = AsyncMock(side_effect=close_tunnel)
        monkeypatch.setattr(NetGateway, "start", start_gateway)
        monkeypatch.setattr("wahlap_bot.bot.CloudflaredTunnel", Mock(return_value=self.tunnel))
        return Settings(
            bot_token="123456:TEST_TOKEN",
            database_path=tmp_path / "tunnel.sqlite3",
            cloudflared_mode=request.param,
            webapp_public_url=self.public_url if request.param == "named" else None,
            cloudflared_tunnel_token="tunnel-secret" if request.param == "named" else None,
        )

    async def test_gateway_binds_before_tunnel_and_main_menu_uses_discovered_url(self, bot):
        assert self.events == ["gateway_start", "tunnel_start"]
        assert bot.handlers.webapp_url == self.public_url
        assert (
            bot.handlers.gateway.origin
            == self.public_url.split("/", 3)[0] + "//" + self.public_url.split("/", 3)[2]
        )
        await configure(bot, 101, "http://127.0.0.1:8080")
        assert web_apps(bot.message(101)) == [self.public_url]
        assert callbacks(bot.message(101)) == {"qrcode", "settings"}
        await bot.send(101, "/net")
        assert web_apps(bot.message(101)) == [self.public_url]
        bot.handlers.client.get_net_url.assert_not_awaited()

    async def test_shutdown_stops_tunnel_before_gateway(self, bot, monkeypatch):
        close_gateway = bot.handlers.gateway.close

        async def close():
            self.events.append("gateway_close")
            await close_gateway()

        monkeypatch.setattr(bot.handlers.gateway, "close", close)
        await bot.app.post_shutdown(bot.app)
        assert self.events == ["gateway_start", "tunnel_start", "tunnel_close", "gateway_close"]
        assert bot.handlers.client.session.closed
        assert bot.handlers.webapp_url is None

    async def test_dead_tunnel_shows_error_without_blocking_qrcode(self, bot, monkeypatch):
        await configure(bot, 101, "http://127.0.0.1:8080")
        self.tunnel.running = False
        await bot.send(101, "/start")
        assert web_apps(bot.message(101)) == []
        assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
        await bot.click(101, "net")
        assert "隧道暂时不可用" in bot.message(101)["text"]
        assert web_apps(bot.message(101)) == []
        bot.handlers.client.get_net_url.assert_not_awaited()
        monkeypatch.setattr(
            bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode())
        )
        await bot.send(101, "/qrcode")
        assert photo_payload(bot, bot.message(101)) == "SGWCMAID_TEST"


@pytest.mark.parametrize("failure", ["gateway", "tunnel"])
async def test_cloudflared_startup_failure_cleans_up_without_exposing_an_occupied_port(
    bot, tmp_path, monkeypatch, failure
):
    start_gateway = AsyncMock(
        side_effect=OSError("port occupied") if failure == "gateway" else None
    )
    tunnel = SimpleNamespace(
        start=AsyncMock(side_effect=CloudflaredError("tunnel unavailable")), close=AsyncMock()
    )
    tunnel_factory = Mock(return_value=tunnel)
    monkeypatch.setattr(NetGateway, "start", start_gateway)
    monkeypatch.setattr("wahlap_bot.bot.CloudflaredTunnel", tunnel_factory)
    telegram_request = type(bot.request)()
    app = create_application(
        Settings(
            bot_token="123456:TEST_TOKEN",
            database_path=tmp_path / "startup.sqlite3",
            cloudflared_mode="quick",
        ),
        bot=ExtBot(token="123456:TEST_TOKEN", request=telegram_request),
    )
    await app.initialize()
    try:
        with pytest.raises(OSError if failure == "gateway" else CloudflaredError):
            await app.post_init(app)
        assert app.bot_data["handlers"].client.session.closed
        with pytest.raises(RuntimeError, match="not open"):
            await app.bot_data["handlers"].store.get(101)
        if failure == "gateway":
            tunnel_factory.assert_not_called()
        else:
            tunnel.close.assert_awaited_once()
    finally:
        await app.shutdown()
        await app.post_shutdown(app)


async def configure(bot, user_id, endpoint, token=None):
    await bot.send(user_id, "/start")
    await bot.send(user_id, endpoint)
    if token is None:
        await bot.click(user_id, "skip_token")
    else:
        await bot.send(user_id, token)


async def test_bot_startup_does_not_read_or_modify_name(bot):
    assert not any(method in {"setMyName", "getMyName"} for method, _ in bot.request.calls)
    assert bot.request.bot_name == "Mai"


async def test_restart_with_matching_telegram_profile_does_not_repeat_writes(bot):
    bot.request.calls.clear()
    sync = BotCommandsSync(bot.app.bot, BOT_COMMANDS)
    await sync.start()
    assert [method for method, _ in bot.request.calls] == ["getMyCommands"]
    await sync.close()


@pytest.mark.parametrize("finish", ["recover", "stop"])
async def test_profile_rate_limit_does_not_abort_startup_or_block_commands(
    bot, tmp_path, monkeypatch, caplog, finish
):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    request = type(bot.request)()
    request.failures["setMyCommands"] = [
        {
            "error_code": 429,
            "description": "Too Many Requests: retry after 600",
            "parameters": {"retry_after": 600},
        }
    ]
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        assert seconds == 600.5
        waiting.set()
        await release.wait()

    monkeypatch.setattr("wahlap_bot.profile._sleep", sleep)
    app = create_application(
        Settings(bot_token="123456:TEST_TOKEN", database_path=tmp_path / "limited.sqlite3"),
        bot=ExtBot(token="123456:TEST_TOKEN", request=request),
    )
    await app.initialize()
    try:
        await asyncio.wait_for(app.post_init(app), 2)
        await asyncio.wait_for(waiting.wait(), 1)
        sync = app.bot_data["handlers"].profile
        assert sync._tasks
        assert not request.commands
        harness = type(bot)(app, request, itertools.count(1))
        await harness.send(101, "/help")
        assert "/qrcode" in harness.message(101)["text"]
        assert "setMyCommands" in caplog.text and "600.5" in caplog.text
        if finish == "recover":
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(sync._tasks)), 1)
            assert request.commands
            assert request.bot_name == "Mai"
        else:
            await app.post_stop(app)
            assert not sync._tasks
            assert sum(method == "setMyCommands" for method, _ in request.calls) == 1
    finally:
        await app.post_stop(app)
        await app.shutdown()
        await app.post_shutdown(app)


async def test_gateway_main_menu_opens_webapp_directly_without_preparing_links(bot, monkeypatch):
    public_url = "https://bot.example.com/net/"
    bot.handlers.settings = replace(bot.handlers.settings, webapp_public_url=public_url)
    get_net = AsyncMock(side_effect=AssertionError("Main menu must not request OAuth"))
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await configure(bot, 101, "https://one.example", "first")
    await configure(bot, 202, "https://two.example", "second")
    for user_id in (101, 202):
        message = bot.message(user_id)
        assert callbacks(message) == {"qrcode", "settings"}
        assert web_apps(message) == [public_url]
        message_id = message["message_id"]
        await bot.click(user_id, "settings")
        await bot.click(user_id, "menu")
        assert bot.message(user_id)["message_id"] == message_id
        assert web_apps(bot.message(user_id)) == [public_url]
    get_net.assert_not_called()


async def test_gateway_net_command_and_old_callback_only_offer_the_fixed_entry(bot, monkeypatch):
    public_url = "https://bot.example.com/net/"
    await configure(bot, 101, "https://one.example")
    old_menu_id = bot.message(101)["message_id"]
    bot.handlers.settings = replace(bot.handlers.settings, webapp_public_url=public_url)
    get_net = AsyncMock(side_effect=AssertionError("Gateway handles authorization after opening"))
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await bot.click(101, "net", message_id=old_menu_id)
    assert bot.message(101)["message_id"] == old_menu_id
    assert callbacks(bot.message(101)) == {"menu"}
    assert web_apps(bot.message(101)) == [public_url]
    await bot.send(101, "/net")
    assert callbacks(bot.message(101)) == {"menu"}
    assert web_apps(bot.message(101)) == [public_url]
    get_net.assert_not_called()


def markup(parameters):
    value = parameters["reply_markup"]
    return json.loads(value) if isinstance(value, str) else value


def callbacks(message):
    return {
        button["callback_data"]
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
        if "callback_data" in button
    }


def web_apps(message):
    return [
        button["web_app"]["url"]
        for row in message.get("reply_markup", {}).get("inline_keyboard", [])
        for button in row
        if "web_app" in button
    ]


def player_qrcode():
    return PlayerQRCode(
        "https://wq.wahlap.net/qrcode/req/MAID_TEST.html",
        "MAID_TEST",
        "把下方二维码对准机台扫描处",
        int(time.time()) + 300,
    )


def photo_payload(bot, message):
    content = bot.request.files[message["photo"][-1]["file_id"]]
    with BytesIO(content) as stream, Image.open(stream) as image:
        code = zxingcpp.read_barcode(image)
    return code.text if code is not None else None


async def test_first_use_requires_explicit_endpoint_and_token_choice(bot):
    await bot.send(101, "/start")
    assert await bot.handlers.store.get(101) is None
    await bot.send(101, "http://127.0.0.1:8080/api/v1/")
    assert await bot.handlers.store.get(101) is None
    await bot.click(101, "skip_token")
    assert await bot.handlers.store.get(101) == UserEndpoint(101, "http://127.0.0.1:8080")


async def test_two_users_setup_and_edit_are_isolated(bot):
    await bot.send(101, "/start")
    await bot.send(202, "/start")
    await bot.send(101, "https://one.example/mai")
    await bot.send(202, "https://two.example")
    await bot.send(101, "one-secret")
    await bot.send(202, "two-secret")
    assert await bot.handlers.store.get(101) == UserEndpoint(
        101, "https://one.example/mai", "one-secret"
    )
    assert await bot.handlers.store.get(202) == UserEndpoint(
        202, "https://two.example", "two-secret"
    )
    await bot.send(101, "/endpoint https://new.example")
    await bot.send(101, "/skip")
    assert await bot.handlers.store.get(101) == UserEndpoint(101, "https://new.example")
    assert (await bot.handlers.store.get(202)).auth_token == "two-secret"
    assert any(name == "deleteMessage" for name, _ in bot.request.calls)
    assert "one-secret" not in json.dumps(bot.request.calls)
    assert "two-secret" not in json.dumps(bot.request.calls)


async def test_cancel_preserves_previous_config(bot):
    await configure(bot, 101, "http://one.example", "original-secret")
    await bot.send(101, "/endpoint")
    await bot.send(101, "https://pending.example")
    await bot.send(101, "/cancel")
    assert await bot.handlers.store.get(101) == UserEndpoint(
        101, "http://one.example", "original-secret"
    )


async def test_invalid_endpoint_can_be_corrected(bot):
    await bot.send(101, "/start")
    await bot.send(101, "missing-scheme:8080")
    assert await bot.handlers.store.get(101) is None
    await bot.send(101, "http://127.0.0.1:8080")
    await bot.send(101, "/skip")
    assert (await bot.handlers.store.get(101)).endpoint == "http://127.0.0.1:8080"


async def test_settings_token_edit_and_reset_only_affect_owner(bot):
    await configure(bot, 101, "http://one.example", "secret")
    await configure(bot, 202, "http://two.example", "other-secret")
    bot.request.calls.clear()
    await bot.send(101, "/settings")
    settings = [params for name, params in bot.request.calls if name == "sendMessage"][-1]
    assert "http://one.example" in settings["text"]
    assert "已设置" in settings["text"]
    assert "secret" not in settings["text"]
    assert settings["protect_content"] is True
    await bot.send(101, "/token")
    await bot.send(101, "/skip")
    assert (await bot.handlers.store.get(101)).auth_token is None
    await bot.send(101, "/reset")
    assert await bot.handlers.store.get(101) is None
    assert (await bot.handlers.store.get(202)).auth_token == "other-secret"


async def test_unconfigured_feature_starts_onboarding(bot, monkeypatch):
    get_qr = AsyncMock()
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", get_qr)
    await bot.send(101, "/qrcode")
    get_qr.assert_not_called()
    assert await bot.handlers.store.get(101) is None
    assert "endpoint" in bot.request.calls[-1][1]["text"]


async def test_qrcode_image_protection_and_refresh_button(bot, monkeypatch):
    await configure(bot, 101, "https://one.example", "secret")
    qr = player_qrcode()
    get_qr = AsyncMock(return_value=qr)
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", get_qr)
    await bot.send(101, "/qrcode")
    get_qr.assert_awaited_once_with(UserEndpoint(101, "https://one.example", "secret"))
    photo = [params for name, params in bot.request.calls if name == "editMessageMedia"][-1]
    message = bot.message(101)
    assert message["has_protected_content"] is True
    assert "北京时间" in message["caption"]
    assert "MAID_TEST" not in message["caption"]
    assert markup(photo)["inline_keyboard"][0][0]["callback_data"] == "qrcode"
    assert callbacks(message) == {"qrcode", "menu"}
    assert all("url" not in button for row in markup(photo)["inline_keyboard"] for button in row)
    bot.request.calls.clear()
    await bot.click(101, "qrcode")
    assert get_qr.await_count == 2
    assert bot.message(101)["message_id"] != message["message_id"]
    assert (101, message["message_id"]) not in bot.request.messages
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "deleteMessage",
        "sendMessage",
        "editMessageMedia",
    ]


async def test_net_url_button_and_user_routing(bot, monkeypatch):
    await configure(bot, 101, "https://one.example", "first")
    await configure(bot, 202, "https://two.example", "second")
    get_net = AsyncMock(
        side_effect=lambda user: f"https://maimai.wahlap.com/maimai-mobile/?t=user-{user.user_id}"
    )
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    bot.request.calls.clear()
    await bot.send(101, "/net")
    await bot.click(202, "net")
    edits = [
        params
        for name, params in bot.request.calls
        if name == "editMessageText" and params.get("reply_markup")
    ]
    assert "user-101" in markup(edits[0])["inline_keyboard"][0][0]["web_app"]["url"]
    assert "user-202" in markup(edits[1])["inline_keyboard"][0][0]["web_app"]["url"]
    assert "url" not in markup(edits[0])["inline_keyboard"][0][0]
    assert get_net.call_args_list[0].args[0].endpoint == "https://one.example"
    assert get_net.call_args_list[1].args[0].auth_token == "second"
    for name, params in bot.request.calls:
        if name == "sendMessage":
            assert params["protect_content"] is True


async def test_user_facing_api_error(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(
        bot.handlers.client, "get_qrcode", AsyncMock(side_effect=MaiserverError("尚未登录微信"))
    )
    await bot.send(101, "/qrcode")
    assert bot.request.calls[-1][0] == "editMessageText"
    assert "尚未登录微信" in bot.request.calls[-1][1]["text"]
    assert not any(name == "sendPhoto" for name, _ in bot.request.calls)


async def test_group_commands_and_unapproved_users_cannot_access_accounts(bot):
    await bot.send(101, "/start", chat_type="group")
    assert await bot.handlers.store.get(101) is None
    assert "私聊" in bot.request.calls[-1][1]["text"]
    assert not bot.app.user_data.get(101)
    bot.handlers.settings = replace(bot.handlers.settings, allowed_user_ids=frozenset({101}))
    await bot.send(202, "/start")
    assert "没有使用" in bot.request.calls[-1][1]["text"]
    assert await bot.handlers.store.get(202) is None


async def test_slow_user_does_not_block_another_user(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    await configure(bot, 202, "http://two.example")
    started = asyncio.Event()
    release = asyncio.Event()

    async def get_net(user):
        if user.user_id == 101:
            started.set()
            await release.wait()
        return f"https://maimai.wahlap.com/maimai-mobile/?t={user.user_id}"

    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    first = asyncio.create_task(bot.send(101, "/net"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(bot.send(202, "/net"), 2)
        assert not first.done()
    finally:
        release.set()
        await first


async def test_menu_settings_and_return_edit_the_same_message(bot):
    await configure(bot, 101, "http://one.example")
    message_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "settings")
    assert bot.message(101)["message_id"] == message_id
    assert "API endpoint" in bot.message(101)["text"]
    assert callbacks(bot.message(101)) == {"endpoint", "token", "menu"}
    await bot.click(101, "menu")
    assert bot.message(101)["message_id"] == message_id
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
    assert web_apps(bot.message(101)) == []
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "editMessageText",
        "answerCallbackQuery",
        "editMessageText",
    ]


async def test_skip_token_replaces_setup_buttons_without_new_message(bot):
    await bot.send(101, "/start")
    message_id = bot.message(101)["message_id"]
    await bot.send(101, "http://one.example")
    assert callbacks(bot.message(101)) == {"skip_token", "cancel"}
    bot.request.calls.clear()
    await bot.click(101, "skip_token")
    assert bot.message(101)["message_id"] == message_id
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
    assert web_apps(bot.message(101)) == []
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery", "editMessageText"]
    bot.request.calls.clear()
    # A delayed second click must not clear the replacement menu.
    await bot.click(101, "skip_token", message_id=message_id)
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery"]
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
    assert web_apps(bot.message(101)) == []


async def test_submitting_token_updates_prompt_and_removes_one_time_buttons(bot):
    await bot.send(101, "/start")
    await bot.send(101, "http://one.example")
    message_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.send(101, "secret")
    assert bot.message(101)["message_id"] == message_id
    assert "skip_token" not in callbacks(bot.message(101))
    assert "cancel" not in callbacks(bot.message(101))
    assert (await bot.handlers.store.get(101)).auth_token == "secret"
    assert [name for name, _ in bot.request.calls] == ["deleteMessage", "editMessageText"]


@pytest.mark.parametrize("stage", ["endpoint", "token"])
async def test_cancel_button_clears_setup_keyboard_in_place(bot, stage):
    await bot.send(101, "/start")
    if stage == "token":
        await bot.send(101, "http://one.example")
    message_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "cancel")
    assert bot.message(101)["message_id"] == message_id
    assert "已取消" in bot.message(101)["text"]
    assert not bot.message(101).get("reply_markup")
    assert await bot.handlers.store.get(101) is None
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery", "editMessageText"]


@pytest.mark.parametrize("command", ["/start", "/endpoint", "/reset"])
async def test_restarting_setup_removes_buttons_from_abandoned_prompt(bot, command):
    await bot.send(101, "/start")
    await bot.send(101, "http://one.example")
    old_id = bot.message(101)["message_id"]
    assert "skip_token" in callbacks(bot.message(101))
    bot.request.calls.clear()
    await bot.send(101, command)
    assert not bot.message(101, old_id).get("reply_markup")
    assert bot.message(101)["message_id"] != old_id
    assert any(name == "editMessageReplyMarkup" for name, _ in bot.request.calls)


@pytest.mark.parametrize("data", ["skip_token", "cancel"])
async def test_old_setup_button_cannot_consume_a_new_draft(bot, data):
    await bot.send(101, "/start")
    await bot.send(101, "http://old.example")
    old_id = bot.message(101)["message_id"]
    await bot.send(101, "/endpoint")
    await bot.send(101, "https://new.example")
    new_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, data, message_id=old_id)
    assert await bot.handlers.store.get(101) is None
    assert "skip_token" in callbacks(bot.message(101, new_id))
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery"]
    await bot.click(101, "skip_token", message_id=new_id)
    assert (await bot.handlers.store.get(101)).endpoint == "https://new.example"


async def test_net_refresh_removes_old_link_while_loading_and_reuses_message(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    old_url = "https://maimai.wahlap.com/maimai-mobile/?t=old"
    new_url = "https://maimai.wahlap.com/maimai-mobile/?t=new"
    monkeypatch.setattr(bot.handlers.client, "get_net_url", AsyncMock(return_value=old_url))
    await bot.click(101, "net")
    message_id = bot.message(101)["message_id"]
    assert old_url in json.dumps(bot.message(101))

    async def get_net(_user):
        current = bot.message(101, message_id)
        assert "正在获取" in current["text"]
        assert not current.get("reply_markup")
        assert old_url not in json.dumps(current)
        return new_url

    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    bot.request.calls.clear()
    await bot.click(101, "net")
    assert bot.message(101)["message_id"] == message_id
    assert new_url in json.dumps(bot.message(101))
    assert old_url not in json.dumps(bot.message(101))
    assert not any(name in {"sendMessage", "sendPhoto"} for name, _ in bot.request.calls)


async def test_returning_to_menu_deletes_qrcode_and_sends_plain_text(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    monkeypatch.setattr(
        bot.handlers.client,
        "get_net_url",
        AsyncMock(return_value="https://maimai.wahlap.com/maimai-mobile/?t=new"),
    )
    await bot.click(101, "qrcode")
    old_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "menu")
    message_id = bot.message(101)["message_id"]
    assert message_id != old_id
    assert (101, old_id) not in bot.request.messages
    assert not bot.message(101).get("photo")
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
    assert web_apps(bot.message(101)) == []
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "deleteMessage",
        "sendMessage",
    ]
    bot.request.calls.clear()
    await bot.click(101, "net")
    assert bot.message(101)["message_id"] == message_id
    assert "点击下方按钮" in bot.message(101)["text"]
    assert "刷新二维码" not in json.dumps(bot.message(101))
    assert callbacks(bot.message(101)) == {"net", "menu"}
    assert not bot.message(101).get("photo")
    await bot.click(101, "menu")
    await bot.click(101, "settings")
    assert bot.message(101)["message_id"] == message_id
    assert "API endpoint" in bot.message(101)["text"]
    assert callbacks(bot.message(101)) == {"endpoint", "token", "menu"}
    assert all(name in {"answerCallbackQuery", "editMessageText"} for name, _ in bot.request.calls)


async def test_failed_qrcode_refresh_keeps_old_image_hidden(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    message_id = bot.message(101)["message_id"]
    monkeypatch.setattr(
        bot.handlers.client, "get_qrcode", AsyncMock(side_effect=MaiserverError("尚未登录微信"))
    )
    bot.request.calls.clear()
    await bot.click(101, "qrcode")
    assert bot.message(101)["message_id"] != message_id
    assert (101, message_id) not in bot.request.messages
    assert "尚未登录微信" in bot.message(101)["text"]
    assert "wahlap.net/qrcode" not in json.dumps(bot.message(101))
    assert not bot.message(101).get("photo")
    assert callbacks(bot.message(101)) == {"qrcode", "menu"}
    assert not any(name in {"editMessageMedia", "sendPhoto"} for name, _ in bot.request.calls)


async def test_repeated_identical_edit_does_not_send_duplicate_message(bot):
    await configure(bot, 101, "http://one.example")
    await bot.click(101, "settings")
    message_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "settings", message_id=message_id)
    assert bot.message(101)["message_id"] == message_id
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery", "editMessageText"]


async def test_uneditable_message_falls_back_and_removes_original_buttons(bot):
    await configure(bot, 101, "http://one.example")
    old_id = bot.message(101)["message_id"]
    bot.request.failures["editMessageText"] = ["Message can't be edited"]
    bot.request.calls.clear()
    await bot.click(101, "settings")
    assert bot.message(101)["message_id"] != old_id
    assert "API endpoint" in bot.message(101)["text"]
    assert not bot.message(101, old_id).get("reply_markup")
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "editMessageText",
        "editMessageReplyMarkup",
        "sendMessage",
    ]


async def test_leaving_setup_clears_its_buttons_and_preserves_saved_config(bot):
    await configure(bot, 101, "http://one.example", "saved-secret")
    menu_id = bot.message(101)["message_id"]
    await bot.send(101, "/token")
    prompt_id = bot.message(101)["message_id"]
    await bot.click(101, "settings", message_id=menu_id)
    assert not bot.message(101, prompt_id).get("reply_markup")
    assert callbacks(bot.message(101, menu_id)) == {"endpoint", "token", "menu"}
    assert (await bot.handlers.store.get(101)).auth_token == "saved-secret"
    assert not bot.app.user_data[101]


async def test_long_settings_use_text_after_leaving_qrcode(bot, monkeypatch):
    endpoint = "https://one.example/" + "a" * 1100
    await configure(bot, 101, endpoint)
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    old_id = bot.message(101)["message_id"]
    await bot.click(101, "menu")
    menu_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "settings")
    assert bot.message(101)["message_id"] == menu_id
    assert endpoint in bot.message(101)["text"]
    assert (101, old_id) not in bot.request.messages
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "editMessageText",
    ]


async def test_inaccessible_callback_message_falls_back_to_a_new_message(bot):
    await configure(bot, 101, "http://one.example")
    old_id = bot.message(101)["message_id"]
    bot.request.calls.clear()
    await bot.click(101, "settings", inaccessible=True)
    assert bot.message(101)["message_id"] != old_id
    assert "API endpoint" in bot.message(101)["text"]
    assert [name for name, _ in bot.request.calls] == ["answerCallbackQuery", "sendMessage"]


async def test_refresh_deletes_old_qrcode_without_uploading_a_placeholder(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    message_id = bot.message(101)["message_id"]
    assert photo_payload(bot, bot.message(101)) == "SGWCMAID_TEST"
    status_ids = []

    async def fetch(_user):
        # This runs before the backend returns: the old image must already be gone.
        assert (101, message_id) not in bot.request.messages
        current = bot.message(101)
        assert "正在获取" in current["text"]
        assert not current.get("photo")
        assert not current.get("reply_markup")
        assert not any(name in {"editMessageMedia", "sendPhoto"} for name, _ in bot.request.calls)
        status_ids.append(current["message_id"])
        return replace(player_qrcode(), maid="MAID_REFRESHED")

    monkeypatch.setattr(bot.handlers.client, "get_qrcode", fetch)
    bot.request.calls.clear()
    await bot.click(101, "qrcode")
    assert bot.message(101)["message_id"] != message_id
    assert bot.message(101)["message_id"] == status_ids[0]
    assert photo_payload(bot, bot.message(101)) == "SGWCMAID_REFRESHED"
    assert sum(name == "editMessageMedia" for name, _ in bot.request.calls) == 1
    assert not any(name == "sendPhoto" for name, _ in bot.request.calls)


@pytest.mark.parametrize("feature", ["qrcode", "net"])
async def test_feature_errors_only_offer_retry_and_main_menu(bot, monkeypatch, feature):
    await configure(bot, 101, "http://one.example")
    method = {"qrcode": "get_qrcode", "net": "get_net_url"}[feature]
    monkeypatch.setattr(
        bot.handlers.client, method, AsyncMock(side_effect=MaiserverError("请求失败"))
    )
    await bot.click(101, feature)
    assert callbacks(bot.message(101)) == {feature, "menu"}
    assert all(
        "url" not in button
        for row in bot.message(101)["reply_markup"]["inline_keyboard"]
        for button in row
    )


@pytest.mark.parametrize("feature", ["qrcode", "net"])
async def test_unexpected_feature_error_only_offers_main_menu(bot, monkeypatch, feature):
    await configure(bot, 101, "http://one.example")
    method = {"qrcode": "get_qrcode", "net": "get_net_url"}[feature]
    monkeypatch.setattr(bot.handlers.client, method, AsyncMock(side_effect=RuntimeError("failure")))
    await bot.click(101, feature)
    assert "操作暂时失败" in bot.message(101)["text"]
    assert callbacks(bot.message(101)) == {"menu"}


async def test_uneditable_status_is_replaced_with_a_new_qrcode(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    old_id = bot.message(101)["message_id"]
    bot.request.failures["editMessageMedia"] = ["Message can't be edited"]

    async def fetch(_user):
        assert (101, old_id) not in bot.request.messages
        return player_qrcode()

    monkeypatch.setattr(bot.handlers.client, "get_qrcode", fetch)
    bot.request.calls.clear()
    await bot.click(101, "qrcode")
    assert bot.message(101)["message_id"] != old_id
    assert photo_payload(bot, bot.message(101)) == "SGWCMAID_TEST"
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "deleteMessage",
        "sendMessage",
        "editMessageMedia",
        "deleteMessage",
        "sendPhoto",
    ]


async def test_unexpected_refresh_error_updates_new_text_status(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    old_id = bot.message(101)["message_id"]
    monkeypatch.setattr(
        bot.handlers.client, "get_qrcode", AsyncMock(side_effect=RuntimeError("failure"))
    )
    bot.request.calls.clear()
    await bot.click(101, "qrcode")
    assert (101, old_id) not in bot.request.messages
    assert len(bot.request.messages) == 1
    assert "操作暂时失败" in bot.message(101)["text"]
    assert not bot.message(101).get("photo")
    assert callbacks(bot.message(101)) == {"menu"}
    assert [name for name, _ in bot.request.calls] == [
        "answerCallbackQuery",
        "deleteMessage",
        "sendMessage",
        "editMessageText",
    ]


@pytest.mark.parametrize("command", ["/start", "/help"])
async def test_main_menu_does_not_prepare_net_links(bot, monkeypatch, command):
    await configure(bot, 101, "http://one.example", "secret")
    get_net = AsyncMock(side_effect=AssertionError("The main menu must not request NET"))
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    bot.request.calls.clear()
    await bot.send(101, command)
    get_net.assert_not_awaited()
    current = bot.message(101)
    assert web_apps(current) == []
    button = current["reply_markup"]["inline_keyboard"][1][0]
    assert button == {"text": "打开 maimai DX NET", "callback_data": "net"}
    assert callbacks(current) == {"qrcode", "net", "settings"}
    assert [name for name, _ in bot.request.calls] == ["sendMessage"]
    assert "secret" not in json.dumps(bot.request.calls)


async def test_net_prepares_each_users_url_only_when_clicked(bot, monkeypatch):
    await configure(bot, 101, "https://one.example", "one")
    await configure(bot, 202, "https://two.example", "two")
    get_net = AsyncMock(
        side_effect=lambda user: f"https://maimai.wahlap.com/maimai-mobile/?t={user.user_id}"
    )
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await bot.send(101, "/start")
    await bot.send(202, "/start")
    get_net.assert_not_awaited()
    await bot.click(101, "net")
    await bot.click(202, "net")
    assert web_apps(bot.message(101)) == ["https://maimai.wahlap.com/maimai-mobile/?t=101"]
    assert web_apps(bot.message(202)) == ["https://maimai.wahlap.com/maimai-mobile/?t=202"]
    assert get_net.call_args_list[0].args[0] == UserEndpoint(101, "https://one.example", "one")
    assert get_net.call_args_list[1].args[0] == UserEndpoint(202, "https://two.example", "two")


async def test_net_links_are_refreshed_on_demand_instead_of_when_returning_to_menu(
    bot, monkeypatch
):
    await configure(bot, 101, "http://one.example")
    old_url = "https://maimai.wahlap.com/maimai-mobile/?t=old"
    new_url = "https://maimai.wahlap.com/maimai-mobile/?t=new"
    get_net = AsyncMock(side_effect=[old_url, new_url])
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await bot.send(101, "/start")
    get_net.assert_not_awaited()
    await bot.click(101, "net")
    assert web_apps(bot.message(101)) == [old_url]
    message_id = bot.message(101)["message_id"]
    await bot.click(101, "menu", message_id=message_id)
    assert bot.message(101)["message_id"] == message_id
    assert web_apps(bot.message(101)) == []
    assert get_net.await_count == 1
    await bot.click(101, "net")
    assert web_apps(bot.message(101)) == [new_url]
    assert get_net.await_count == 2
    assert old_url not in json.dumps(bot.message(101))


async def test_unavailable_net_does_not_affect_main_menu_or_setup(bot, monkeypatch):
    get_net = AsyncMock(side_effect=MaiserverError("暂时不可用"))
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await configure(bot, 101, "http://one.example")
    await bot.send(101, "/start")
    get_net.assert_not_awaited()
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}
    assert web_apps(bot.message(101)) == []
    url = "https://maimai.wahlap.com/maimai-mobile/?t=recovered"
    monkeypatch.setattr(bot.handlers.client, "get_net_url", AsyncMock(return_value=url))
    await bot.click(101, "net")
    assert web_apps(bot.message(101)) == [url]
    assert callbacks(bot.message(101)) == {"net", "menu"}


async def test_slow_net_does_not_delay_the_main_menu(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")

    async def slow(_user):
        await asyncio.Event().wait()

    get_net = AsyncMock(side_effect=slow)
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await asyncio.wait_for(bot.send(101, "/start"), 1)
    get_net.assert_not_awaited()
    assert callbacks(bot.message(101)) == {"qrcode", "net", "settings"}


async def test_returning_from_qrcode_deletes_photo_without_requesting_net(bot, monkeypatch):
    await configure(bot, 101, "http://one.example")
    monkeypatch.setattr(bot.handlers.client, "get_qrcode", AsyncMock(return_value=player_qrcode()))
    await bot.click(101, "qrcode")
    old_id = bot.message(101)["message_id"]

    get_net = AsyncMock(side_effect=AssertionError("Returning to the menu must not request NET"))
    monkeypatch.setattr(bot.handlers.client, "get_net_url", get_net)
    await bot.click(101, "menu")
    get_net.assert_not_awaited()
    assert (101, old_id) not in bot.request.messages
    assert web_apps(bot.message(101)) == []
    assert not bot.message(101).get("photo")
