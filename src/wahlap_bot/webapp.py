"""Authenticate Telegram Mini Apps and proxy each user's maiserver NET session."""

import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from functools import partial
from importlib.resources import files
from urllib.parse import parse_qsl, unquote, urlsplit

import aiohttp
from aiohttp import web

from .config import Settings, normalize_webapp_url
from .endpoints import UserEndpoint
from .logging_utils import describe_error
from .maiserver import MaiserverClient
from .net_cache import AssetCache, AssetKey, ProxyPayload
from .storage import UserStore

APP_TITLE = "maimai DX NET"
logger = logging.getLogger(__name__)
PROXY_PATH = "/api/v1/maimai-dx/reverse-proxy"
GAME_ORIGIN = "https://maimai.wahlap.com"
COOKIE_NAME = "__Secure-wahlap-net"
SESSION_TTL = 3600
MAX_PROXY_BYTES = 8 * 1024 * 1024
NET_SCRIPT = files("wahlap_bot").joinpath("static/net.js").read_text(encoding="utf-8")
PROXY_SCRIPT = files("wahlap_bot").joinpath("static/proxy.js").read_text(encoding="utf-8")
STATIC_PATH = re.compile(
    r"^(?:maimai-mobile/)?(?:(?:css|js|img|images|fonts?|assets)/.+"
    r"\.(?:css|m?js|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|eot)"
    r"|apple-touch-icon(?:-precomposed)?\.png|favicon\.ico)$",
    re.IGNORECASE,
)
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class WebAppAuthError(ValueError):
    pass


class _ProxyResponseTooLarge(Exception):
    pass


def _static_request(tail: str, query: str) -> bool:
    if not STATIC_PATH.fullmatch(tail):
        return False
    # Static files may carry version keys, but not account selectors or actions.
    try:
        return all(
            key.lower() in {"ver", "v", "version", "hash"}
            for key, _ in parse_qsl(
                query, keep_blank_values=True, strict_parsing=True, max_num_fields=16
            )
        )
    except ValueError:
        return False


def _static_response(payload: ProxyPayload, tail: str) -> bool:
    mime = payload.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    extension = tail.rsplit(".", 1)[-1].lower()
    expected = {
        "css": {"text/css"},
        "js": {"text/javascript", "application/javascript", "application/x-javascript"},
        "mjs": {"text/javascript", "application/javascript"},
        "png": {"image/png"},
        "jpg": {"image/jpeg"},
        "jpeg": {"image/jpeg"},
        "gif": {"image/gif"},
        "svg": {"image/svg+xml"},
        "webp": {"image/webp"},
        "ico": {"image/x-icon", "image/vnd.microsoft.icon"},
        "woff": {"font/woff", "application/font-woff"},
        "woff2": {"font/woff2"},
        "ttf": {"font/ttf", "application/font-sfnt", "application/x-font-ttf"},
        "otf": {"font/otf", "application/font-sfnt", "application/x-font-opentype"},
        "eot": {"application/vnd.ms-fontobject"},
    }
    return (
        payload.status == 200
        and bool(payload.body)
        and "location" not in payload.headers
        and "content-range" not in payload.headers
        and not {"*", "cookie", "authorization", "x-auth-token"}.intersection(
            part.strip().lower() for part in payload.headers.get("vary", "").split(",")
        )
        and mime in expected.get(extension, set())
    )


def validate_init_data(value: str, bot_token: str, *, now: float | None = None) -> int:
    if not isinstance(value, str) or len(value) > 16384:
        raise WebAppAuthError("Telegram 身份信息无效，请重新打开小程序。")
    try:
        pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        supplied_hash = fields.pop("hash")
        check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.digest(key, check.encode(), "sha256").hex()
        if not hmac.compare_digest(expected, supplied_hash):
            raise ValueError
        auth_date = int(fields["auth_date"])
        age = (time.time() if now is None else now) - auth_date
        if not -30 <= age <= 300:
            raise ValueError
        user_id = json.loads(fields["user"])["id"]
        if type(user_id) is not int or user_id <= 0:
            raise ValueError
        return user_id
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise WebAppAuthError("Telegram 身份校验失败或已过期，请关闭后重新打开小程序。") from exc


def _account_fingerprint(user: UserEndpoint) -> str:
    return hashlib.sha256(f"{user.endpoint}\0{user.auth_token or ''}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NetSession:
    user_id: int
    account_fingerprint: str
    cookie_secret: str = field(repr=False)
    expires_at: float


class SessionStore:
    def __init__(self):
        self.sessions: dict[str, NetSession] = {}

    def create(self, user: UserEndpoint) -> tuple[str, NetSession]:
        now = time.time()
        self.sessions = {
            sid: session for sid, session in self.sessions.items() if session.expires_at > now
        }
        owned = [sid for sid, session in self.sessions.items() if session.user_id == user.user_id]
        for sid in owned[:-7]:
            del self.sessions[sid]
        sid = secrets.token_urlsafe(24)
        session = NetSession(
            user.user_id, _account_fingerprint(user), secrets.token_urlsafe(32), now + SESSION_TTL
        )
        self.sessions[sid] = session
        return sid, session

    def get(self, sid: str, cookie: str) -> NetSession:
        session = self.sessions.get(sid)
        if (
            session is None
            or session.expires_at <= time.time()
            or not hmac.compare_digest(session.cookie_secret, cookie)
        ):
            raise WebAppAuthError("小程序会话已失效，请从机器人重新打开。")
        return session

    def revoke(self, user_id: int) -> None:
        self.sessions = {
            sid: session for sid, session in self.sessions.items() if session.user_id != user_id
        }


def _base_headers() -> dict[str, str]:
    return {
        "cache-control": "private, no-store",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "content-security-policy": "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
    }


def _page(message: str, status: int = 200) -> web.Response:
    body = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"<title>{APP_TITLE}</title></head>"
        '<body style="margin:24px;font-family:system-ui;overflow-wrap:anywhere">'
        f"<p>{html.escape(message)}</p></body></html>"
    )
    return web.Response(text=body, status=status, content_type="text/html", headers=_base_headers())


def _bootstrap(prefix: str) -> str:
    endpoint = json.dumps(prefix + "session").replace("<", "\\u003c")
    script = NET_SCRIPT.replace("SESSION_ENDPOINT", endpoint)
    return """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>maimai DX NET</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>html,body{margin:0;width:100%;height:100%;font-family:system-ui}
#stage{position:fixed;inset:0;overflow:hidden}
.net-frame{position:absolute;top:0;left:0;border:0;transform-origin:top left;visibility:hidden}
#error{position:fixed;inset:0;z-index:1;padding:24px;background:var(--tg-theme-bg-color,#fff);overflow-wrap:anywhere}
button{font:inherit;padding:8px 20px}</style></head><body>
<div id="stage"></div>
<div id="error" hidden role="alert"><p id="error-message"></p><button id="retry">重试</button></div>
<script>NET_SCRIPT</script></body></html>""".replace("NET_SCRIPT", script)


def _safe_tail(tail: str) -> str:
    value = tail
    for _ in range(6):
        if (
            "\\" in value
            or any(ord(char) < 32 for char in value)
            or "?" in value
            or "#" in value
            or any(segment in {".", ".."} for segment in value.split("/"))
        ):
            raise ValueError("Invalid proxy path")
        decoded = unquote(value, errors="strict")
        if decoded == value:
            return tail
        value = decoded
    raise ValueError("Excessively encoded proxy path")


def rewrite_text(value: str, scope: str, *, is_html: bool = False) -> str:
    for origin in (GAME_ORIGIN, "http://maimai.wahlap.com", "//maimai.wahlap.com"):
        value = value.replace(origin, scope)
    value = value.replace(PROXY_PATH, scope)

    def scoped(path: str) -> str:
        return path if path == scope or path.startswith(scope + "/") else scope + path

    value = re.sub(
        r"(?i)(url\(\s*)([\"']?)(/(?!/)[^\"')]+)\2(\s*\))",
        lambda match: match[1] + match[2] + scoped(match[3]) + match[2] + match[4],
        value,
    )
    if not is_html:
        return value
    value = re.sub(r"(?is)<title\b[^>]*>.*?</title\s*>", "", value)
    value = re.sub(r"(?is)<meta\b(?=[^>]*\bname\s*=\s*['\"]?viewport\b)[^>]*>", "", value)
    value = re.sub(
        r"(?i)(\b(?:href|src|action|poster)\s*=\s*)([\"'])(/(?!/)[^\"']*)\2",
        lambda match: match[1] + match[2] + scoped(match[3]) + match[2],
        value,
    )
    scope_json = json.dumps(scope).replace("<", "\\u003c")
    patch = (
        f"<title>{APP_TITLE}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        "<script>" + PROXY_SCRIPT.replace("PROXY_SCOPE", scope_json) + "</script>"
    )
    if re.search(r"(?i)<head\b[^>]*>", value):
        return re.sub(r"(?i)<head\b[^>]*>", lambda match: match[0] + patch, value, count=1)
    return patch + value


class NetGateway:
    def __init__(self, settings: Settings, store: UserStore, client: MaiserverClient):
        self.settings = settings
        self.store = store
        self.client = client
        if not settings.webapp_public_url and settings.cloudflared_mode != "quick":
            raise ValueError("WEBAPP_PUBLIC_URL is required for the NET gateway")
        self.prefix = "/net/"
        self.origin: str | None = None
        if settings.webapp_public_url:
            public = urlsplit(normalize_webapp_url(settings.webapp_public_url))
            self.prefix = public.path
            self.set_public_url(settings.webapp_public_url)
        self.sessions = SessionStore()
        self.cache = AssetCache(
            settings.webapp_cache_max_mb * 1024 * 1024, settings.webapp_cache_ttl
        )
        self.runner: web.AppRunner | None = None

    def revoke_user(self, user_id: int) -> None:
        self.sessions.revoke(user_id)
        self.cache.invalidate(user_id)

    def set_public_url(self, value: str) -> None:
        public = urlsplit(normalize_webapp_url(value))
        if public.path != self.prefix:
            raise ValueError("隧道公开路径与网关监听路径不一致。")
        self.origin = f"{public.scheme}://{public.netloc}"

    def application(self) -> web.Application:
        @web.middleware
        async def log_errors(request, handler):
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as exc:
                logger.error("NET 网关异常 error=%s", describe_error(exc))
                raise

        app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[log_errors])
        app.router.add_get(self.prefix, self.bootstrap)
        app.router.add_post(self.prefix + "session", self.create_session)
        app.router.add_route("*", self.prefix + "p/{sid}/{tail:.*}", self.proxy)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.application(), access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(
                self.runner, self.settings.webapp_listen_host, self.settings.webapp_listen_port
            ).start()
            logger.info(
                "NET 网关已监听 port=%s cache=%sMiB ttl=%ss",
                self.settings.webapp_listen_port,
                self.settings.webapp_cache_max_mb,
                self.settings.webapp_cache_ttl,
            )
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        await self.cache.close()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.sessions.sessions.clear()

    async def _fetch(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> ProxyPayload:
        async with self.client.session.request(
            method,
            url,
            headers=headers,
            data=body,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=self.settings.api_timeout),
        ) as upstream:
            response_body = bytearray()
            if upstream.status not in {401, 403}:
                async for chunk in upstream.content.iter_chunked(65536):
                    response_body.extend(chunk)
                    if len(response_body) > MAX_PROXY_BYTES:
                        raise _ProxyResponseTooLarge
            connection_headers = {
                field.strip().lower()
                for field in upstream.headers.get("Connection", "").split(",")
                if field.strip()
            }
            forwarded = {
                key.lower(): value
                for key, value in upstream.headers.items()
                if key.lower()
                not in HOP_HEADERS
                | connection_headers
                | {
                    "set-cookie",
                    "content-length",
                    "content-encoding",
                    "content-md5",
                    "etag",
                    "last-modified",
                    "content-security-policy",
                    "x-frame-options",
                    "access-control-allow-origin",
                    "access-control-allow-credentials",
                    "authorization",
                    "x-auth-token",
                    "refresh",
                    "cdn-cache-control",
                    "cloudflare-cdn-cache-control",
                    "surrogate-control",
                }
            }
            payload = ProxyPayload(
                upstream.status, forwarded, bytes(response_body), upstream.charset
            )
        return payload

    async def bootstrap(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=_bootstrap(self.prefix), content_type="text/html", headers=_base_headers()
        )

    async def create_session(self, request: web.Request) -> web.Response:
        if self.origin is None:
            return web.json_response(
                {"error": "HTTPS 隧道正在准备，请稍后重新打开。"},
                status=503,
                headers=_base_headers(),
            )
        if request.headers.get("Origin") != self.origin:
            return web.json_response(
                {"error": "请求来源无效。"}, status=403, headers=_base_headers()
            )
        try:
            data = await request.json()
            user_id = validate_init_data(data["init_data"], self.settings.bot_token)
        except (ValueError, KeyError, TypeError, UnicodeError):
            logger.warning("NET 身份校验失败")
            return web.json_response(
                {"error": "Telegram 身份校验失败，请关闭后从机器人重新打开。"},
                status=401,
                headers=_base_headers(),
            )
        if self.settings.allowed_user_ids and user_id not in self.settings.allowed_user_ids:
            logger.warning("NET 拒绝访问 user=%s reason=无权限", user_id)
            return web.json_response(
                {"error": "此账号无权使用。"}, status=403, headers=_base_headers()
            )
        user = await self.store.get(user_id)
        if user is None:
            logger.info("NET 拒绝访问 user=%s reason=未配置账号", user_id)
            return web.json_response(
                {"error": "请先私聊机器人发送 /start 设置 endpoint。"},
                status=403,
                headers=_base_headers(),
            )
        sid, session = self.sessions.create(user)
        logger.info("NET 会话已创建 user=%s", user_id)
        scope = self.prefix + "p/" + sid + "/"
        response = web.json_response(
            {"path": scope + "maimai-mobile/home/", "scope": scope}, headers=_base_headers()
        )
        response.set_cookie(
            COOKIE_NAME,
            session.cookie_secret,
            path=scope,
            max_age=SESSION_TTL,
            secure=True,
            httponly=True,
            samesite="None",
        )
        return response

    async def proxy(self, request: web.Request) -> web.Response:
        try:
            session = self.sessions.get(
                request.match_info["sid"], request.cookies.get(COOKIE_NAME, "")
            )
        except WebAppAuthError as exc:
            logger.debug("NET 会话无效")
            return _page(str(exc), 401)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("Origin") != self.origin
        ):
            return _page("请求来源无效。", 403)
        user = await self.store.get(session.user_id)
        if user is None or _account_fingerprint(user) != session.account_fingerprint:
            logger.debug("NET 会话配置已失效 user=%s", session.user_id)
            return _page("账号设置已变更，请从机器人重新打开。", 401)
        if self.settings.allowed_user_ids and session.user_id not in self.settings.allowed_user_ids:
            return _page("此账号无权使用。", 403)
        try:
            tail = _safe_tail(request.match_info["tail"])
        except (ValueError, UnicodeError):
            return _page("无效的页面路径。", 400)
        scope = self.prefix + "p/" + request.match_info["sid"]
        url = user.api_url("maimai-dx/reverse-proxy/") + tail
        if request.rel_url.raw_query_string:
            url += "?" + request.rel_url.raw_query_string
        headers = user.headers | {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            in {"accept", "content-type", "if-none-match", "if-modified-since", "range"}
        }
        if request.headers.get("Origin"):
            headers["Origin"] = GAME_ORIGIN
        if request.headers.get("Referer"):
            headers["Referer"] = GAME_ORIGIN + "/" + tail
        body = await request.read() if request.can_read_body else None
        candidate = _static_request(tail, request.rel_url.raw_query_string)
        cacheable = partial(_static_response, tail=tail)
        use_cache = (
            candidate
            and request.method == "GET"
            and not request.can_read_body
            and "Range" not in request.headers
        )
        if use_cache:
            # A cold application cache needs a complete body, not a browser's 304.
            headers = {
                key: value
                for key, value in headers.items()
                if key.lower() not in {"if-none-match", "if-modified-since"}
            }
        load = partial(self._fetch, request.method, url, headers, body)
        started = time.monotonic()
        try:
            remaining = self.settings.webapp_cache_ttl
            cache_state = "BYPASS"
            if use_cache:
                variant = tuple(
                    sorted(
                        (key.lower(), value)
                        for key, value in headers.items()
                        if key.lower() != "x-auth-token"
                    )
                )
                key = AssetKey(user.user_id, session.account_fingerprint, url, variant)
                cached, cache_state = await self.cache.get(key, load, cacheable)
                payload = cached.payload
                remaining = cached.expires_at - time.monotonic()
            else:
                payload = await load()
            logger.debug(
                "NET 请求 user=%s method=%s status=%s cache=%s elapsed=%.2fs",
                user.user_id,
                request.method,
                payload.status,
                cache_state,
                time.monotonic() - started,
            )
            if payload.status >= 400:
                logger.warning(
                    "NET 回源失败 user=%s status=%s elapsed=%.2fs",
                    user.user_id,
                    payload.status,
                    time.monotonic() - started,
                )
            if payload.status in {401, 403}:
                return _page("maiserver 认证失败，请在机器人中用 /token 检查 Token。", 502)
            forwarded = dict(payload.headers)
            location = forwarded.get("location")
            if location:
                location = rewrite_text(location, scope)
                parsed = urlsplit(location)
                if parsed.scheme or parsed.netloc:
                    logger.warning("NET 回源失败 user=%s reason=站外跳转", user.user_id)
                    return _page("后端返回了站外跳转，请重新打开小程序。", 502)
                if (
                    location.startswith("/")
                    and location != scope
                    and not location.startswith(scope + "/")
                ):
                    location = scope + location
                forwarded["location"] = location
            content_type = forwarded.get("content-type", "")
            response_body = payload.body
            if any(kind in content_type for kind in ("text/", "javascript", "json", "xml")):
                text = response_body.decode(payload.charset or "utf-8", errors="replace")
                text = rewrite_text(text, scope, is_html="text/html" in content_type)
                response_body = text.encode()
                forwarded["content-type"] = content_type.split(";", 1)[0] + "; charset=utf-8"
            forwarded.update(_base_headers())
            forwarded["x-net-cache"] = cache_state
            if candidate and request.method in {"GET", "HEAD"} and cacheable(payload):
                max_age = int(min(remaining, session.expires_at - time.time()))
                if max_age > 0:
                    forwarded["cache-control"] = f"private, max-age={max_age}"
                    for key in ("expires", "pragma", "date", "age"):
                        forwarded.pop(key, None)
            return web.Response(body=response_body, status=payload.status, headers=forwarded)
        except _ProxyResponseTooLarge:
            logger.warning("NET 回源失败 user=%s reason=响应过大", user.user_id)
            return _page("后端响应过大。", 502)
        except TimeoutError:
            logger.warning(
                "NET 回源超时 user=%s elapsed=%.2fs", user.user_id, time.monotonic() - started
            )
            return _page("maiserver 响应超时，请稍后重试。", 504)
        except aiohttp.ClientError as exc:
            logger.warning("NET 连接失败 user=%s error=%s", user.user_id, describe_error(exc))
            return _page("暂时无法连接 maiserver，请检查机器人中的 endpoint。", 502)
