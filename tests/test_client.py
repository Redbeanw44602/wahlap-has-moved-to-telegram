"""Transport-independent API contract tests; the real HTTP/WS tests are separate."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest

from wahlap_bot.endpoints import UserEndpoint
from wahlap_bot.maiserver import MAX_RESPONSE_BYTES, MaiserverClient, MaiserverError


def data():
    return {
        "url": "https://wq.wahlap.net/qrcode/req/MAID_TEST.html",
        "maid": "MAID_TEST",
        "description": "玩家二维码",
        "expires_in": int(time.time()) + 300,
    }


class Response:
    def __init__(self, payload=None, *, status=200, body=None):
        self.status = status
        self.body = json.dumps(payload).encode() if body is None else body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index : index + size]


class WebSocket:
    def __init__(self, messages, *, delay=0):
        self.messages = messages
        self.delay = delay
        self.commands = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send_json(self, command):
        self.commands.append(command)

    async def __aiter__(self):
        for payload in self.messages:
            if self.delay:
                await asyncio.sleep(self.delay)
            if callable(payload):
                payload = payload(self.commands[-1])
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload))


async def test_net_requests_use_each_users_endpoint_and_token():
    url = "https://maimai.wahlap.com/maimai-mobile/?t=authorization"
    session = SimpleNamespace(get=Mock(return_value=Response({"code": 0, "data": {"url": url}})))
    client = MaiserverClient(session)
    assert await client.get_net_url(UserEndpoint(101, "http://one.example/mai", "one")) == url
    assert await client.get_net_url(UserEndpoint(202, "http://two.example", "two")) == url
    assert session.get.call_count == 2
    first, second = session.get.call_args_list
    assert first.args == ("http://one.example/mai/api/v1/auth/aimetoken/maimai-dx",)
    assert first.kwargs["headers"] == {"X-Auth-Token": "one"}
    assert first.kwargs["allow_redirects"] is False
    assert second.args == ("http://two.example/api/v1/auth/aimetoken/maimai-dx",)
    assert second.kwargs["headers"] == {"X-Auth-Token": "two"}


async def test_net_requires_https_for_telegram_web_app():
    session = SimpleNamespace(
        get=Mock(return_value=Response({"code": 0, "data": {"url": "http://example.com/net"}}))
    )
    with pytest.raises(MaiserverError, match="HTTPS"):
        await MaiserverClient(session).get_net_url(UserEndpoint(101, "http://one.example"))


async def test_websocket_ignores_unrelated_messages_and_closes_after_fresh_push():
    ws = WebSocket(
        [
            {"type": "broadcast", "sub_type": "unrelated", "data": {}},
            lambda command: {"type": "reply", "echo": command["echo"] + 1, "code": 6},
            {"type": "broadcast", "sub_type": "qrcode_received", "data": data()},
        ]
    )
    session = SimpleNamespace(ws_connect=Mock(return_value=ws), get=Mock())
    result = await MaiserverClient(session).get_qrcode(
        UserEndpoint(101, "http://one.example", "one")
    )
    assert result.maid == "MAID_TEST"
    assert ws.commands[0]["path"] == "/auth/qrcode"
    assert type(ws.commands[0]["echo"]) is int
    assert 0 < ws.commands[0]["echo"] < 2**32
    assert ws.closed
    assert session.ws_connect.call_args.args == ("http://one.example/api/v1/stream",)
    assert session.ws_connect.call_args.kwargs["headers"] == {"X-Auth-Token": "one"}
    session.get.assert_not_called()


@pytest.mark.parametrize("status", [404, 405, 426, 501])
async def test_unsupported_stream_falls_back_to_rest(status):
    session = SimpleNamespace(
        ws_connect=Mock(side_effect=aiohttp.WSServerHandshakeError(None, (), status=status)),
        get=Mock(return_value=Response({"code": 0, "data": data()})),
    )
    qr = await MaiserverClient(session).get_qrcode(
        UserEndpoint(101, "http://one.example", "secret")
    )
    assert qr.maid == "MAID_TEST"
    assert session.get.call_args.args == ("http://one.example/api/v1/auth/qrcode",)
    assert session.get.call_args.kwargs["headers"] == {"X-Auth-Token": "secret"}


async def test_stream_authentication_failure_has_no_rest_retry():
    session = SimpleNamespace(
        ws_connect=Mock(side_effect=aiohttp.WSServerHandshakeError(None, (), status=401)),
        get=Mock(),
    )
    with pytest.raises(MaiserverError, match="认证失败"):
        await MaiserverClient(session).get_qrcode(UserEndpoint(101, "http://one.example"))
    session.get.assert_not_called()


async def test_stream_timeout_is_bounded_and_connection_closed():
    ws = WebSocket([{"type": "broadcast"}], delay=1)
    session = SimpleNamespace(ws_connect=Mock(return_value=ws))
    with pytest.raises(MaiserverError, match="超时"):
        await MaiserverClient(session, timeout=0.01).get_qrcode(
            UserEndpoint(101, "http://one.example")
        )
    assert ws.closed


async def test_stream_backend_error_is_returned_immediately():
    ws = WebSocket([lambda command: {"type": "reply", "echo": command["echo"], "code": 6}])
    session = SimpleNamespace(ws_connect=Mock(return_value=ws))
    with pytest.raises(MaiserverError, match="尚未登录微信"):
        await MaiserverClient(session).get_qrcode(UserEndpoint(101, "http://one.example"))
    assert ws.closed


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"code": 1}, "超时"),
        ({"code": 6}, "尚未登录微信"),
        ({"code": 7}, "缺少微信会话"),
        ({"code": 30}, "登录 maimai DX NET 失败"),
        ({"code": 999, "message": "secret response"}, "错误码 999"),
        ({"code": 0}, "缺少 data"),
        ({"code": False, "data": {}}, "格式不正确"),
        ([], "格式不正确"),
        ({"code": 0, "data": {"url": "javascript:alert(1)"}}, "无效的网页地址"),
    ],
)
async def test_api_errors_do_not_reveal_upstream_body(payload, message):
    session = SimpleNamespace(get=Mock(return_value=Response(payload)))
    with pytest.raises(MaiserverError, match=message) as error:
        await MaiserverClient(session).get_net_url(UserEndpoint(101, "http://one.example"))
    assert "secret response" not in str(error.value)


@pytest.mark.parametrize("status", [302, 401, 403, 404, 429, 500])
async def test_failed_http_status(status):
    session = SimpleNamespace(get=Mock(return_value=Response(status=status)))
    with pytest.raises(MaiserverError):
        await MaiserverClient(session).get_net_url(UserEndpoint(101, "http://one.example"))


@pytest.mark.parametrize("body", [b"<html>not JSON</html>", b"x" * (MAX_RESPONSE_BYTES + 1)])
async def test_invalid_or_oversized_body(body):
    session = SimpleNamespace(get=Mock(return_value=Response(body=body)))
    with pytest.raises(MaiserverError):
        await MaiserverClient(session).get_net_url(UserEndpoint(101, "http://one.example"))
