"""Async maiserver REST and streaming clients. Never visit NET authorization URLs."""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import urlsplit

import aiohttp
import qrcode

from .endpoints import UserEndpoint
from .logging_utils import DiagnosticError, describe_error

MAX_RESPONSE_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


class MaiserverError(DiagnosticError):
    """A safe message that can be shown to the user without leaking credentials."""


class _StreamUnavailable(Exception):
    pass


def create_api_session() -> aiohttp.ClientSession:
    async def reject_redirect(*_args: object) -> None:
        # aiohttp's WebSocket handshake otherwise follows redirects and forwards
        # custom authentication headers. Require the final endpoint instead.
        raise MaiserverError(
            "API 地址发生重定向，请用 /endpoint 设置最终服务地址。", reason="redirect"
        )

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        trace_configs=[trace],
        headers={"User-Agent": "wahlap-telegram-bot/0.1"},
    )


def _http_error(status: int) -> MaiserverError:
    if 300 <= status < 400:
        message, reason = "API 地址发生重定向，请用 /endpoint 设置最终服务地址。", "redirect"
    elif status in {401, 403}:
        message, reason = (
            "API 认证失败，请用 /token 设置或检查 X-Auth-Token。",
            "authentication_failed",
        )
    elif status == 404:
        message, reason = (
            "找不到 API，请用 /endpoint 检查 maiserver 地址及路径前缀。",
            "api_not_found",
        )
    elif status == 429:
        message, reason = "API 请求过于频繁，请稍后重试。", "rate_limited"
    else:
        message, reason = f"maiserver 返回 HTTP {status}，请检查服务状态。", "http_error"
    return MaiserverError(message, reason=reason, http_status=status)


def _unwrap(payload: object) -> dict:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise MaiserverError(
            "API 响应格式不正确，请确认 endpoint 指向 maiserver。", reason="invalid_response_schema"
        )
    code = payload["code"]
    if code != 0:
        messages = {
            1: "maiserver 等待微信响应超时，请稍后重试。",
            6: "maiserver 尚未登录微信，请先在对应实例中登录。",
            7: "maiserver 缺少微信会话信息，请检查对应实例的登录状态。",
            30: "maiserver 登录 maimai DX NET 失败，请稍后重试。",
        }
        # Upstream error bodies may contain URLs or credentials; do not echo them.
        raise MaiserverError(
            messages.get(code, f"maiserver 返回错误码 {code}，请检查服务日志。"),
            reason="api_error",
            api_code=code,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MaiserverError("API 响应缺少 data 字段。", reason="missing_response_data")
    return data


def _web_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise MaiserverError("API 返回了无效的网页地址。", reason="invalid_web_url")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
        ):
            raise ValueError
        _ = parts.port
    except ValueError as exc:
        raise MaiserverError("API 返回了无效的网页地址。", reason="invalid_web_url") from exc
    return value


@dataclass(frozen=True, slots=True)
class PlayerQRCode:
    url: str = field(repr=False)
    maid: str = field(repr=False)
    description: str
    expires_at: int

    @classmethod
    def from_data(cls, data: dict) -> "PlayerQRCode":
        maid = data.get("maid")
        expiry = data.get("expires_in")  # Despite its name this is a Unix timestamp.
        if (
            not isinstance(maid, str)
            or not maid.startswith("MAID")
            or not 4 < len(maid) <= 1024
            or not maid.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in maid)
            or type(expiry) is not int
            or not 0 < expiry <= 4102444800
        ):
            raise MaiserverError("API 返回的二维码数据不完整。", reason="invalid_qr_data")
        if expiry <= time.time():
            raise MaiserverError(
                "收到的二维码已过期，请点击「刷新二维码」重新获取。", reason="qr_expired"
            )
        description = data.get("description", "")
        if not isinstance(description, str):
            raise MaiserverError(
                "API 返回的二维码说明格式不正确。", reason="invalid_qr_description"
            )
        return cls(_web_url(data.get("url")), maid, description, expiry)

    def png(self) -> BytesIO:
        # Verified by decoding the PNG linked from the official QR code page.
        # Game scanners expect SGWC + MAID, including the literal MAID prefix.
        code = qrcode.QRCode(box_size=10, border=4)
        code.add_data("SGWC" + self.maid)
        code.make(fit=True)
        image = code.make_image(fill_color="black", back_color="white")
        output = BytesIO()
        output.name = "player-qrcode.png"
        image.save(output, format="PNG")
        output.seek(0)
        return output


class MaiserverClient:
    def __init__(self, session: aiohttp.ClientSession, timeout: float = 25.0):
        self.session = session
        self.timeout = timeout

    async def _get(self, user: UserEndpoint, path: str) -> dict:
        try:
            async with self.session.get(
                user.api_url(path),
                headers=user.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise _http_error(response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise MaiserverError(
                            "API 响应过大，请检查 endpoint。", reason="response_too_large"
                        )
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise MaiserverError(
                        "API 未返回有效 JSON，请检查 endpoint。", reason="invalid_json"
                    ) from exc
                return _unwrap(payload)
        except TimeoutError as exc:
            raise MaiserverError(
                "连接 maiserver 超时，请检查服务后重试。", reason="request_timeout"
            ) from exc
        except aiohttp.ClientError as exc:
            raise MaiserverError(
                "无法连接 maiserver，请检查 endpoint、网络及 TLS 证书。", reason="connection_failed"
            ) from exc

    async def get_net_url(self, user: UserEndpoint) -> str:
        data = await self._get(user, "auth/aimetoken/maimai-dx")
        url = _web_url(data.get("url"))
        if urlsplit(url).scheme != "https":
            raise MaiserverError(
                "NET 地址必须使用 HTTPS，才能在 Telegram 内打开。", reason="https_required"
            )
        return url

    async def get_qrcode(self, user: UserEndpoint) -> PlayerQRCode:
        try:
            data = await self._stream_qrcode(user)
        except _StreamUnavailable as exc:
            # Compatibility with older instances/proxies without WebSocket support.
            logger.debug(
                "QR WebSocket unavailable; using REST user=%s error=%s",
                user.user_id,
                describe_error(exc),
            )
            data = await self._get(user, "auth/qrcode")
        return PlayerQRCode.from_data(data)

    async def _stream_qrcode(self, user: UserEndpoint) -> dict:
        echo = secrets.randbelow(2**32 - 1) + 1
        try:
            # One short-lived connection per request. No cross-user subscriptions or cache.
            async with asyncio.timeout(self.timeout):
                async with self.session.ws_connect(
                    user.api_url("stream"),
                    headers=user.headers,
                    max_msg_size=MAX_RESPONSE_BYTES,
                    timeout=aiohttp.ClientWSTimeout(ws_close=1.0),
                ) as websocket:
                    await websocket.send_json({"path": "/auth/qrcode", "echo": echo})
                    async for message in websocket:
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise MaiserverError(
                                "二维码推送连接中断，请重试。", reason="qr_stream_interrupted"
                            )
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(message.data)
                        except (ValueError, UnicodeError) as exc:
                            raise MaiserverError(
                                "二维码推送格式不正确。", reason="invalid_qr_stream_data"
                            ) from exc
                        if not isinstance(payload, dict):
                            raise MaiserverError(
                                "二维码推送格式不正确。", reason="invalid_qr_stream_data"
                            )
                        if payload.get("type") == "reply":
                            if payload.get("echo") not in (None, echo):
                                continue
                            data = _unwrap(payload)
                            if "maid" in data:
                                return data
                        if payload.get("type") == "broadcast" and payload.get("sub_type") in (
                            "qrcode_received",
                            "qrcode-received",
                        ):
                            data = payload.get("data")
                            if not isinstance(data, dict):
                                raise MaiserverError(
                                    "二维码推送缺少数据。", reason="missing_qr_stream_data"
                                )
                            return data
                    raise MaiserverError(
                        "二维码推送连接已关闭，请重试。", reason="qr_stream_closed"
                    )
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in {404, 405, 426, 501}:
                raise _StreamUnavailable from exc
            raise _http_error(exc.status) from exc
        except TimeoutError as exc:
            raise MaiserverError(
                "等待新的二维码超时，请确认微信已登录后重试。", reason="qr_stream_timeout"
            ) from exc
        except aiohttp.ClientError as exc:
            raise MaiserverError(
                "无法连接二维码推送接口，请检查 endpoint 和网络。",
                reason="qr_stream_connection_failed",
            ) from exc
