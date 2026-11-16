import asyncio
import time

import pytest
import zxingcpp
from aiohttp import web
from PIL import Image

from wahlap_bot.endpoints import UserEndpoint
from wahlap_bot.maiserver import MaiserverClient, MaiserverError, PlayerQRCode, create_api_session


def qr_data(**changes):
    data = {
        "url": "https://wq.wahlap.net/qrcode/req/MAID_TEST.html",
        "maid": "MAID_TEST",
        "description": "把下方二维码对准机台扫描处",
        "expires_in": int(time.time()) + 300,
    }
    return data | changes


async def test_stream_fresh_qrcode_prefix_auth_and_echo(serve):
    received = []

    async def stream(request):
        received.append(request.headers.get("X-Auth-Token"))
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        command = await websocket.receive_json()
        assert command["path"] == "/auth/qrcode"
        assert 0 < command["echo"] < 2**32
        await websocket.send_json({"type": "broadcast", "sub_type": "unrelated", "data": {}})
        await websocket.send_json(
            {"type": "reply", "echo": command["echo"] + 1, "code": 6, "message": "ignore"}
        )
        await websocket.send_json(
            {"type": "broadcast", "sub_type": "qrcode_received", "data": qr_data()}
        )
        return websocket

    app = web.Application()
    app.router.add_get("/prefix/api/v1/stream", stream)
    base = await serve(app)
    async with create_api_session() as session:
        qr = await MaiserverClient(session).get_qrcode(UserEndpoint(1, base + "/prefix", "secret"))
    assert qr.maid == "MAID_TEST"
    assert received == ["secret"]
    with qr.png() as output, Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width == image.height
        assert image.width > 200


async def test_rest_fallback_and_token_isolation(serve):
    received = []

    async def rest(request):
        received.append((request.headers.get("X-Auth-Token"), request.headers.get("Cookie")))
        result = web.json_response({"code": 0, "data": qr_data()})
        result.set_cookie("account", "must-not-share")
        return result

    app = web.Application()
    app.router.add_get("/api/v1/auth/qrcode", rest)
    base = await serve(app)
    async with create_api_session() as session:
        client = MaiserverClient(session)
        await client.get_qrcode(UserEndpoint(1, base, "one"))
        await client.get_qrcode(UserEndpoint(2, base, "two"))
        await client.get_qrcode(UserEndpoint(3, base))
    assert received == [("one", None), ("two", None), (None, None)]


async def test_net_returns_authorized_url_without_visiting_it(serve):
    visited = []

    async def net(request):
        assert request.headers.get("X-Auth-Token") == "net-secret"
        return web.json_response(
            {"code": 0, "data": {"url": "https://maimai.wahlap.com/maimai-mobile/?t=secret"}}
        )

    async def unexpected(request):
        visited.append(request.path)
        return web.Response(status=500)

    app = web.Application()
    app.router.add_get("/api/v1/auth/aimetoken/maimai-dx", net)
    app.router.add_route("*", "/{anything:.*}", unexpected)
    base = await serve(app)
    async with create_api_session() as session:
        url = await MaiserverClient(session).get_net_url(UserEndpoint(1, base, "net-secret"))
    assert url == "https://maimai.wahlap.com/maimai-mobile/?t=secret"
    assert visited == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"code": 6, "message": "secret-upstream-body"}, "尚未登录微信"),
        ({"code": 1}, "等待微信响应超时"),
        ({"code": 7}, "缺少微信会话"),
        ({"code": 30}, "登录 maimai DX NET 失败"),
        ({"code": 999, "message": "secret-upstream-body"}, "999"),
        ({"code": 0}, "缺少 data"),
        ({"code": False, "data": {}}, "格式不正确"),
        ([], "格式不正确"),
        ({"code": 0, "data": {"url": "javascript:alert(1)"}}, "无效的网页地址"),
    ],
)
async def test_upstream_errors_and_invalid_responses(serve, payload, message):
    async def net(request):
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/api/v1/auth/aimetoken/maimai-dx", net)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError, match=message) as error:
            await MaiserverClient(session).get_net_url(UserEndpoint(1, base))
    assert "secret-upstream-body" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_http_errors(serve, status):
    async def net(request):
        return web.Response(status=status)

    app = web.Application()
    app.router.add_get("/api/v1/auth/aimetoken/maimai-dx", net)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError):
            await MaiserverClient(session).get_net_url(UserEndpoint(1, base))


async def test_websocket_auth_error_does_not_fallback(serve):
    paths = []

    async def reject(request):
        paths.append(request.path)
        return web.Response(status=401)

    app = web.Application()
    app.router.add_route("*", "/{anything:.*}", reject)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError, match="认证失败"):
            await MaiserverClient(session).get_qrcode(UserEndpoint(1, base))
    assert paths == ["/api/v1/stream"]


async def test_stream_wechat_not_logged_in(serve):
    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        command = await ws.receive_json()
        await ws.send_json({"type": "reply", "echo": command["echo"], "code": 6})
        return ws

    app = web.Application()
    app.router.add_get("/api/v1/stream", stream)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError, match="尚未登录微信"):
            await MaiserverClient(session).get_qrcode(UserEndpoint(1, base))


async def test_stream_timeout_is_bounded(serve):
    async def stream(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.receive()
        return ws

    app = web.Application()
    app.router.add_get("/api/v1/stream", stream)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError, match="超时"):
            async with asyncio.timeout(2):
                await MaiserverClient(session, timeout=0.05).get_qrcode(UserEndpoint(1, base))


async def test_websocket_redirect_does_not_forward_token(serve):
    visited = []

    async def destination(request):
        visited.append(request.headers.get("X-Auth-Token"))
        return web.Response(status=500)

    async def redirect(request):
        raise web.HTTPFound("/destination")

    app = web.Application()
    app.router.add_get("/api/v1/stream", redirect)
    app.router.add_get("/destination", destination)
    base = await serve(app)
    async with create_api_session() as session:
        with pytest.raises(MaiserverError, match="重定向"):
            await MaiserverClient(session).get_qrcode(UserEndpoint(1, base, "secret"))
    assert visited == []


@pytest.mark.parametrize(
    "changes",
    [
        {"maid": "https://example.com"},
        {"maid": "MAID\nunsafe"},
        {"expires_in": "tomorrow"},
        {"expires_in": True},
        {"expires_in": 10**50},
        {"url": "file:///etc/passwd"},
    ],
)
def test_bad_qrcode_data(changes):
    with pytest.raises(MaiserverError):
        PlayerQRCode.from_data(qr_data(**changes))


def test_reject_expired_qrcode():
    with pytest.raises(MaiserverError, match="已过期"):
        PlayerQRCode.from_data(qr_data(expires_in=int(time.time()) - 1))


def test_generated_png_decodes_to_official_payload_format():
    qr = PlayerQRCode.from_data(qr_data())
    with qr.png() as output, Image.open(output) as image:
        decoded = zxingcpp.read_barcode(image)
    assert decoded is not None
    assert decoded.text == "SGWCMAID_TEST"
