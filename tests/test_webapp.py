import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import aiohttp
import pytest
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict
from yarl import URL

from wahlap_bot.config import Settings
from wahlap_bot.endpoints import UserEndpoint
from wahlap_bot.storage import UserStore
from wahlap_bot.webapp import (
    APP_TITLE,
    COOKIE_NAME,
    GAME_ORIGIN,
    PROXY_PATH,
    NetGateway,
    SessionStore,
    WebAppAuthError,
    rewrite_text,
    validate_init_data,
)

BOT_TOKEN = "123456:TEST_TOKEN"
ORIGIN = "https://bot.example.com"
FIRST = UserEndpoint(101, "https://one.example/mai", "first-secret")
SECOND = UserEndpoint(202, "https://two.example", "second-secret")


def signed_data(user_id=101, *, token=BOT_TOKEN, auth_date=None):
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "user": json.dumps({"id": user_id, "first_name": "玩家"}, ensure_ascii=False),
        "query_id": "query-101",
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def request(*, method="POST", path="/net/session", headers=None, data=None, **kwargs):
    return SimpleNamespace(
        method=method,
        headers=CIMultiDict({"Origin": ORIGIN} if headers is None else headers),
        json=AsyncMock(return_value=data),
        rel_url=URL(path, encoded=True),
        **kwargs,
    )


class Upstream:
    def __init__(self, body=b"page", status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = CIMultiDict({"Content-Type": "text/plain"} if headers is None else headers)
        self.charset = "utf-8"
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def iter_chunked(self, _size):
        yield self.body


@pytest.fixture
async def gateway(tmp_path):
    store = UserStore(tmp_path / "bot.sqlite3")
    await store.open()
    await store.save(FIRST)
    await store.save(SECOND)
    client = SimpleNamespace(session=SimpleNamespace(request=Mock(return_value=Upstream())))
    gateway = NetGateway(
        Settings(bot_token=BOT_TOKEN, webapp_public_url=ORIGIN + "/net/"),
        store,
        client,
    )
    try:
        yield gateway
    finally:
        await gateway.close()
        await store.close()


def proxy_request(gateway, user=FIRST, *, tail="maimai-mobile/home/", **kwargs):
    sid, session = gateway.sessions.create(user)
    return request(
        method=kwargs.pop("method", "GET"),
        path=kwargs.pop("path", f"/net/p/{sid}/{tail}"),
        match_info={"sid": sid, "tail": tail},
        cookies={COOKIE_NAME: session.cookie_secret},
        can_read_body=bool(kwargs.get("body")),
        read=AsyncMock(return_value=kwargs.pop("body", b"")),
        **kwargs,
    )


async def test_gateway_logs_sessions_and_failures_without_auth_or_urls(gateway, caplog):
    caplog.set_level("INFO", logger="wahlap_bot")
    response = await gateway.create_session(request(data={"init_data": signed_data()}))
    assert response.status == 200
    gateway.client.session.request.return_value = Upstream(b"private-body", 429)
    browser_request = proxy_request(gateway)
    assert (await gateway.proxy(browser_request)).status == 429
    assert "NET session created user=101" in caplog.text
    assert "NET upstream failed user=101 status=429" in caplog.text
    for secret in (
        FIRST.endpoint,
        FIRST.auth_token,
        BOT_TOKEN,
        signed_data(),
        "private-body",
        browser_request.match_info["sid"],
        browser_request.cookies[COOKIE_NAME],
    ):
        assert secret not in caplog.text


async def test_cache_request_logs_are_debug_only_and_exclude_paths(gateway, caplog):
    caplog.set_level("INFO", logger="wahlap_bot")
    browser_request = proxy_request(gateway, tail="maimai-mobile/img/private-title.png")
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    await gateway.proxy(browser_request)
    assert "NET request" not in caplog.text
    caplog.set_level("DEBUG", logger="wahlap_bot")
    await gateway.proxy(browser_request)
    assert "status=200 cache=HIT elapsed=" in caplog.text
    assert "private-title" not in caplog.text


async def test_gateway_unexpected_errors_are_logged_without_raw_tracebacks(gateway, caplog):
    middleware = gateway.application().middlewares[0]
    handler = AsyncMock(side_effect=RuntimeError("private-url-and-token"))
    with pytest.raises(RuntimeError):
        await middleware(make_mocked_request("GET", "/net/"), handler)
    assert "NET gateway failed error=RuntimeError" in caplog.text
    assert "private" not in caplog.text
    assert not caplog.records[-1].exc_info


async def test_authentication_logs_explain_signature_failure_without_init_data(gateway, caplog):
    response = await gateway.create_session(
        request(data={"init_data": signed_data(token="secret")})
    )
    assert response.status == 401
    assert "stage=validate_init_data" in caplog.text and "reason=hash_mismatch" in caplog.text
    assert "secret" not in caplog.text and "query-101" not in caplog.text
    assert all(record.getMessage().isascii() for record in caplog.records)


def test_signed_telegram_identity():
    assert validate_init_data(signed_data(), BOT_TOKEN) == 101


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hash=x",
        None,
        "x" * 16385,
        signed_data(token="wrong-token"),
        signed_data().replace("101", "202"),
        signed_data() + "&auth_date=1",
        signed_data(auth_date=1),
        signed_data(auth_date=int(time.time()) + 60),
        signed_data(user_id=True),
        signed_data(user_id="101"),
        signed_data(user_id=-1),
        signed_data(auth_date="not-a-date"),
    ],
)
def test_rejects_tampered_expired_or_malformed_telegram_identity(value):
    with pytest.raises(WebAppAuthError):
        validate_init_data(value, BOT_TOKEN)


async def test_session_uses_signed_identity_and_keeps_credentials_on_server(gateway):
    response = await gateway.create_session(request(data={"init_data": signed_data()}))
    assert response.status == 200
    path = json.loads(response.text)["path"]
    assert path.startswith("/net/p/") and path.endswith("/maimai-mobile/home/")
    assert FIRST.endpoint not in response.text and FIRST.auth_token not in response.text
    assert "101" not in path
    cookie = response.cookies[COOKIE_NAME]
    assert json.loads(response.text)["scope"] == cookie["path"]
    assert cookie["secure"] and cookie["httponly"]
    assert cookie["samesite"] == "None"
    assert path.startswith(cookie["path"]) and cookie["path"].endswith("/")
    assert int(cookie["max-age"]) == 3600
    assert "no-store" in response.headers["Cache-Control"]
    gateway.client.session.request.assert_not_called()


@pytest.mark.parametrize(
    ("data", "headers", "status"),
    [
        ({"init_data": signed_data()}, {}, 403),
        ({"init_data": signed_data()}, {"Origin": "https://evil.example"}, 403),
        ({"init_data": "wrong"}, {"Origin": ORIGIN}, 401),
        ([], {"Origin": ORIGIN}, 401),
        ({"init_data": signed_data(999)}, {"Origin": ORIGIN}, 403),
    ],
)
async def test_invalid_session_requests_never_contact_maiserver(gateway, data, headers, status):
    response = await gateway.create_session(request(data=data, headers=headers))
    assert response.status == status
    assert not gateway.sessions.sessions
    gateway.client.session.request.assert_not_called()


async def test_gateway_enforces_bot_user_allowlist(gateway):
    gateway.settings = replace(gateway.settings, allowed_user_ids=frozenset({202}))
    response = await gateway.create_session(request(data={"init_data": signed_data()}))
    assert response.status == 403
    response = await gateway.proxy(proxy_request(gateway))
    assert response.status == 403
    gateway.client.session.request.assert_not_called()


async def test_each_session_calls_owners_endpoint_and_auth_header(gateway):
    for user in (FIRST, SECOND):
        browser_request = proxy_request(
            gateway,
            user,
            headers={
                "Cookie": "injected=browser-cookie",
                "X-Auth-Token": "injected-token",
                "Authorization": "injected-authorization",
                "Accept": "text/html",
            },
        )
        response = await gateway.proxy(browser_request)
        assert response.status == 200
        args, kwargs = gateway.client.session.request.call_args
        assert args == ("GET", user.api_url("maimai-dx/reverse-proxy/maimai-mobile/home/"))
        assert kwargs["headers"] == {"X-Auth-Token": user.auth_token, "Accept": "text/html"}
        assert kwargs["allow_redirects"] is False


async def test_session_requires_matching_cookie_and_path(gateway):
    first = proxy_request(gateway)
    second = proxy_request(gateway, SECOND)
    first.cookies[COOKIE_NAME] = second.cookies[COOKIE_NAME]
    assert (await gateway.proxy(first)).status == 401
    second.cookies.clear()
    assert (await gateway.proxy(second)).status == 401
    second.match_info["sid"] = "unknown"
    assert (await gateway.proxy(second)).status == 401
    gateway.client.session.request.assert_not_called()


@pytest.mark.parametrize("change", ["endpoint", "token", "reset", "expires"])
async def test_sessions_are_revoked_on_expiry_or_account_changes(gateway, change):
    browser_request = proxy_request(gateway)
    if change == "reset":
        await gateway.store.delete(FIRST.user_id)
    elif change == "expires":
        sid = browser_request.match_info["sid"]
        gateway.sessions.sessions[sid] = replace(gateway.sessions.sessions[sid], expires_at=0)
    else:
        user = (
            replace(FIRST, endpoint="https://changed.example")
            if change == "endpoint"
            else replace(FIRST, auth_token="new-token")
        )
        await gateway.store.save(user)
    assert (await gateway.proxy(browser_request)).status == 401
    gateway.client.session.request.assert_not_called()


def test_session_store_bounds_per_user_without_revoking_other_users():
    sessions = SessionStore()
    other_sid, other = sessions.create(SECOND)
    oldest_sid, oldest = sessions.create(FIRST)
    for _ in range(8):
        sessions.create(FIRST)
    assert len(sessions.sessions) == 9
    assert sessions.get(other_sid, other.cookie_secret) == other
    with pytest.raises(WebAppAuthError):
        sessions.get(oldest_sid, oldest.cookie_secret)


async def test_post_body_query_and_game_origin_are_preserved(gateway):
    browser_request = proxy_request(
        gateway,
        method="POST",
        path="/net/p/session/maimai-mobile/submit/?name=one%26two&x=%2B",
        tail="maimai-mobile/submit/",
        headers={
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/net/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=b"choice=1&name=player",
    )
    assert (await gateway.proxy(browser_request)).status == 200
    args, kwargs = gateway.client.session.request.call_args
    assert args[1].endswith("/maimai-mobile/submit/?name=one%26two&x=%2B")
    assert kwargs["data"] == b"choice=1&name=player"
    assert kwargs["headers"]["Origin"] == GAME_ORIGIN
    assert kwargs["headers"]["Referer"].startswith(GAME_ORIGIN + "/")


async def test_foreign_origin_post_is_rejected(gateway):
    response = await gateway.proxy(
        proxy_request(gateway, method="POST", headers={"Origin": "https://evil.example"})
    )
    assert response.status == 403
    gateway.client.session.request.assert_not_called()


@pytest.mark.parametrize(
    "tail",
    [
        "../auth/qrcode",
        "a/./b",
        "%2e%2e/auth",
        "%252e%252e/auth",
        "x\\y",
        "x%5cy",
        "x%3fy",
        "x%23y",
        "x\x00y",
    ],
)
async def test_proxy_path_cannot_escape_to_other_api_endpoints(gateway, tail):
    response = await gateway.proxy(proxy_request(gateway, tail=tail))
    assert response.status == 400
    gateway.client.session.request.assert_not_called()


def test_html_rewriting_keeps_links_assets_forms_and_js_in_session_scope():
    source = """<html><head><title>old</title><meta name="viewport" content="width=720">
    <link href="/maimai-mobile/style.css"><style>body{background:url('/assets/a.png')}</style>
    </head><body><a href="https://maimai.wahlap.com/maimai-mobile/record/">Next</a>
    <img src="/assets/q.png"><form action="/maimai-mobile/submit/" method="POST"></form>
    <script>var dir = "/api/v1/maimai-dx/reverse-proxy"; var url = dir + "/maimai-mobile/";</script>
    </body></html>"""
    scope = "/net/p/session"
    result = rewrite_text(source, scope, is_html=True)
    assert result.count("<title>maimai DX NET</title>") == 1
    assert result.count('name="viewport"') == 1
    assert 'content="width=device-width,initial-scale=1,viewport-fit=cover"' in result
    for path in (
        "/maimai-mobile/style.css",
        "/assets/a.png",
        "/assets/q.png",
        "/maimai-mobile/record/",
        "/maimai-mobile/submit/",
    ):
        assert scope + path in result
    assert f'var dir = "{scope}"; var url = dir + "/maimai-mobile/";' in result
    assert PROXY_PATH not in result and GAME_ORIGIN not in result
    assert "XMLHttpRequest.prototype.open" in result and "new Request" in result
    assert "submit" in result and "click" in result
    assert result.index("XMLHttpRequest.prototype.open") < result.index("var dir")


def test_css_and_already_scoped_urls_are_rewritten_once():
    result = rewrite_text(
        "a{background:url(/assets/a.png)}b{background:url('/net/p/session/assets/b.png')}",
        "/net/p/session",
    )
    assert "url(/net/p/session/assets/a.png)" in result
    assert "url('/net/p/session/assets/b.png')" in result
    assert "/net/p/session/net/p/session" not in result
    assert APP_TITLE in rewrite_text("<html><head></head></html>", "/scope", is_html=True)


async def test_html_response_is_patched_and_unsafe_or_stale_headers_are_removed(gateway):
    gateway.client.session.request.return_value = Upstream(
        b'<html><head><title>old</title></head><body><img src="/a.png"></body></html>',
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Connection": "keep-alive, X-Hop",
            "X-Hop": "hop-data",
            "Set-Cookie": "game-account=private",
            "X-Auth-Token": "do-not-return",
            "Content-Encoding": "gzip",
            "Content-Security-Policy": "frame-ancestors 'none'",
            "X-Frame-Options": "DENY",
            "ETag": "old-content",
            "Cache-Control": "public",
        },
    )
    browser_request = proxy_request(gateway)
    response = await gateway.proxy(browser_request)
    assert response.status == 200
    assert APP_TITLE in response.text
    assert f"/net/p/{browser_request.match_info['sid']}/a.png" in response.text
    for header in (
        "Set-Cookie",
        "X-Auth-Token",
        "Content-Encoding",
        "ETag",
        "X-Frame-Options",
        "X-Hop",
    ):
        assert header not in response.headers
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in response.headers["Cache-Control"]
    assert "'self'" in response.headers["Content-Security-Policy"]


async def test_binary_response_passes_through_without_changes(gateway):
    image = b"\x89PNG\r\n\x1a\n\x00binary"
    gateway.client.session.request.return_value = Upstream(
        image, headers={"Content-Type": "image/png"}
    )
    response = await gateway.proxy(proxy_request(gateway))
    assert response.body == image
    assert response.headers["Content-Type"] == "image/png"


@pytest.mark.parametrize(
    ("tail", "mime"),
    [
        ("maimai-mobile/css/common.css", "text/css"),
        ("maimai-mobile/js/jquery.min.js", "application/javascript"),
        ("maimai-mobile/img/title.png", "image/png"),
        ("assets/fonts/font.woff2", "font/woff2"),
        ("maimai-mobile/apple-touch-icon.png", "image/png"),
        ("favicon.ico", "image/x-icon"),
    ],
)
async def test_static_assets_can_use_private_browser_cache_within_session(gateway, tail, mime):
    gateway.client.session.request.return_value = Upstream(
        b"static-data",
        headers={
            "Content-Type": mime,
            "Cache-Control": "public, max-age=86400",
            "CDN-Cache-Control": "public",
            "Cloudflare-CDN-Cache-Control": "public",
            "Surrogate-Control": "max-age=86400",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
    browser_request = proxy_request(gateway, tail=tail)
    sid = browser_request.match_info["sid"]
    gateway.sessions.sessions[sid] = replace(
        gateway.sessions.sessions[sid], expires_at=time.time() + 12.5
    )
    response = await gateway.proxy(browser_request)
    policy = response.headers["Cache-Control"]
    assert policy.startswith("private, max-age=")
    assert 0 < int(policy.split("=")[1]) <= 12
    assert "public" not in policy
    for name in (
        "CDN-Cache-Control",
        "Cloudflare-CDN-Cache-Control",
        "Surrogate-Control",
        "Pragma",
        "Expires",
    ):
        assert name not in response.headers


@pytest.mark.parametrize(
    ("tail", "mime", "method", "status"),
    [
        ("maimai-mobile/home/", "text/html", "GET", 200),
        ("maimai-mobile/player.json", "application/json", "GET", 200),
        ("maimai-mobile/profile/avatar.png", "image/png", "GET", 200),
        ("maimai-mobile/css/login.css", "text/html", "GET", 200),
        ("maimai-mobile/css/common.css", "text/css", "GET", 500),
        ("maimai-mobile/css/common.css", "text/css", "POST", 200),
    ],
)
async def test_account_pages_errors_and_mutations_remain_uncacheable(
    gateway, tail, mime, method, status
):
    gateway.client.session.request.return_value = Upstream(
        b"account-data", status, {"Content-Type": mime, "Cache-Control": "public"}
    )
    response = await gateway.proxy(proxy_request(gateway, tail=tail, method=method))
    assert response.headers["Cache-Control"] == "private, no-store"


async def test_cached_asset_urls_are_distinct_between_users(gateway):
    tail = "maimai-mobile/img/title.png"
    first = proxy_request(gateway, FIRST, tail=tail)
    second = proxy_request(gateway, SECOND, tail=tail)
    assert first.rel_url != second.rel_url
    assert first.cookies[COOKIE_NAME] != second.cookies[COOKIE_NAME]


async def test_backend_cache_reuses_raw_asset_across_sessions_and_rewrites_each_scope(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"a{background:url(/maimai-mobile/img/button.png)}", headers={"Content-Type": "text/css"}
    )
    first = proxy_request(gateway, tail="maimai-mobile/css/common.css")
    second = proxy_request(gateway, tail="maimai-mobile/css/common.css")
    response1 = await gateway.proxy(first)
    response2 = await gateway.proxy(second)
    assert response1.headers["X-Net-Cache"] == "MISS"
    assert response2.headers["X-Net-Cache"] == "HIT"
    assert first.match_info["sid"] in response1.text
    assert second.match_info["sid"] in response2.text
    assert first.match_info["sid"] not in response2.text
    gateway.client.session.request.assert_called_once()


async def test_backend_cache_isolates_users_even_with_identical_endpoint_and_token(gateway):
    other = replace(FIRST, user_id=SECOND.user_id)
    await gateway.store.save(other)
    gateway.client.session.request.side_effect = [
        Upstream(b"one", headers={"Content-Type": "image/png"}),
        Upstream(b"two", headers={"Content-Type": "image/png"}),
    ]
    path = "maimai-mobile/img/title.png"
    first = await gateway.proxy(proxy_request(gateway, FIRST, tail=path))
    second = await gateway.proxy(proxy_request(gateway, other, tail=path))
    assert first.body == b"one" and second.body == b"two"
    assert first.headers["X-Net-Cache"] == second.headers["X-Net-Cache"] == "MISS"
    assert gateway.client.session.request.call_count == 2


async def test_auth_and_account_revocation_are_checked_before_cache_hit(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    request = proxy_request(gateway, tail="maimai-mobile/img/title.png")
    assert (await gateway.proxy(request)).status == 200
    assert gateway.cache.entries
    gateway.settings = replace(gateway.settings, allowed_user_ids=frozenset({202}))
    assert (await gateway.proxy(request)).status == 403
    gateway.settings = replace(gateway.settings, allowed_user_ids=frozenset())
    gateway.revoke_user(FIRST.user_id)
    assert not gateway.cache.entries
    assert (await gateway.proxy(request)).status == 401
    gateway.client.session.request.assert_called_once()


@pytest.mark.parametrize("change", ["endpoint", "token"])
async def test_changed_account_cannot_reuse_previous_assets(gateway, change):
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    path = "maimai-mobile/img/title.png"
    assert (await gateway.proxy(proxy_request(gateway, tail=path))).headers["X-Net-Cache"] == "MISS"
    user = (
        replace(FIRST, endpoint="https://changed.example")
        if change == "endpoint"
        else replace(FIRST, auth_token="changed-token")
    )
    await gateway.store.save(user)
    assert (await gateway.proxy(proxy_request(gateway, user, tail=path))).headers[
        "X-Net-Cache"
    ] == "MISS"
    assert gateway.client.session.request.call_count == 2


async def test_cache_preserves_query_versions_and_accept_variants(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    for version, accept, state in (
        ("1", "image/png", "MISS"),
        ("2", "image/png", "MISS"),
        ("1", "image/webp", "MISS"),
        ("1", "image/png", "HIT"),
    ):
        request = proxy_request(
            gateway, tail="maimai-mobile/img/title.png", headers={"Accept": accept}
        )
        request.rel_url = request.rel_url.with_query(v=version)
        assert (await gateway.proxy(request)).headers["X-Net-Cache"] == state
    assert gateway.client.session.request.call_count == 3


async def test_partial_or_head_response_does_not_poison_full_get_cache(gateway):
    gateway.client.session.request.side_effect = [
        Upstream(b"par", 206, {"Content-Type": "image/png", "Content-Range": "bytes 0-2/5"}),
        Upstream(b"", headers={"Content-Type": "image/png"}),
        Upstream(b"image", headers={"Content-Type": "image/png"}),
    ]
    path = "maimai-mobile/img/title.png"
    ranged = await gateway.proxy(proxy_request(gateway, tail=path, headers={"Range": "bytes=0-2"}))
    head = await gateway.proxy(proxy_request(gateway, tail=path, method="HEAD"))
    full = await gateway.proxy(proxy_request(gateway, tail=path))
    cached = await gateway.proxy(proxy_request(gateway, tail=path))
    assert ranged.status == 206 and ranged.headers["X-Net-Cache"] == "BYPASS"
    assert head.headers["X-Net-Cache"] == "BYPASS"
    assert full.body == cached.body == b"image" and cached.headers["X-Net-Cache"] == "HIT"
    assert gateway.client.session.request.call_count == 3


async def test_cold_cache_requests_full_body_instead_of_forwarding_browser_validators(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    response = await gateway.proxy(
        proxy_request(
            gateway,
            tail="maimai-mobile/img/title.png",
            headers={"If-None-Match": "old-etag", "If-Modified-Since": "yesterday"},
        )
    )
    assert response.status == 200 and response.body == b"image"
    headers = gateway.client.session.request.call_args.kwargs["headers"]
    assert "If-None-Match" not in headers and "If-Modified-Since" not in headers


async def test_dynamic_pages_and_failed_assets_are_never_stored(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"page", headers={"Content-Type": "text/html"}
    )
    for _ in range(2):
        response = await gateway.proxy(proxy_request(gateway))
        assert response.headers["X-Net-Cache"] == "BYPASS"
    gateway.client.session.request.return_value = Upstream(
        b"error", 500, {"Content-Type": "image/png"}
    )
    for _ in range(2):
        response = await gateway.proxy(proxy_request(gateway, tail="maimai-mobile/img/title.png"))
        assert response.status == 500 and response.headers["X-Net-Cache"] == "MISS"
    assert not gateway.cache.entries
    assert gateway.client.session.request.call_count == 4


async def test_parallel_asset_requests_share_a_single_upstream_fetch(gateway, monkeypatch):
    started, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class PendingImage(Upstream):
        async def iter_chunked(self, _size):
            started.set()
            await release.wait()
            yield self.body

    gateway.client.session.request.return_value = PendingImage(
        b"image", headers={"Content-Type": "image/png"}
    )
    original = gateway.cache.get
    calls = 0

    async def observe(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            joined.set()
        return await original(*args)

    monkeypatch.setattr(gateway.cache, "get", observe)
    first = asyncio.create_task(
        gateway.proxy(proxy_request(gateway, tail="maimai-mobile/img/title.png"))
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(
            gateway.proxy(proxy_request(gateway, tail="maimai-mobile/img/title.png"))
        )
        await asyncio.wait_for(joined.wait(), 2)
        gateway.client.session.request.assert_called_once()
        release.set()
        one, two = await asyncio.gather(first, second)
        assert one.body == two.body == b"image"
        assert one.headers["X-Net-Cache"] == "MISS" and two.headers["X-Net-Cache"] == "COALESCED"
    finally:
        release.set()
        await first


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_429_is_returned_without_retry_delay_or_cache(gateway, method):
    gateway.client.session.request.side_effect = [
        Upstream(b"limited", 429, {"Content-Type": "text/plain"}),
        Upstream(b"image", headers={"Content-Type": "image/png"}),
    ]
    path = "maimai-mobile/img/title.png"
    first = await gateway.proxy(proxy_request(gateway, tail=path, method=method))
    assert first.status == 429 and first.body == b"limited"
    assert "Retry-After" not in first.headers
    assert first.headers["Cache-Control"] == "private, no-store"
    gateway.client.session.request.assert_called_once()
    assert not gateway.cache.entries
    second = await gateway.proxy(proxy_request(gateway, tail=path))
    assert second.status == 200 and second.body == b"image"
    assert gateway.client.session.request.call_count == 2
    cached = await gateway.proxy(proxy_request(gateway, tail=path))
    assert cached.headers["X-Net-Cache"] == "HIT"
    assert gateway.client.session.request.call_count == 2


async def test_distinct_uncached_assets_start_without_gateway_concurrency_limit(gateway):
    started, release = asyncio.Event(), asyncio.Event()
    active = 0

    class PendingImage(Upstream):
        async def iter_chunked(self, _size):
            nonlocal active
            active += 1
            if active == 3:
                started.set()
            await release.wait()
            yield self.body

    gateway.client.session.request.side_effect = lambda *_args, **_kwargs: PendingImage(
        b"image", headers={"Content-Type": "image/png"}
    )
    tasks = [
        asyncio.create_task(
            gateway.proxy(proxy_request(gateway, tail=f"maimai-mobile/img/{i}.png"))
        )
        for i in range(3)
    ]
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert gateway.client.session.request.call_count == 3
        release.set()
        responses = await asyncio.gather(*tasks)
        assert all(response.status == 200 for response in responses)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_browser_cache_lifetime_follows_remaining_backend_entry_lifetime(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    path = "maimai-mobile/img/title.png"
    await gateway.proxy(proxy_request(gateway, tail=path))
    key, entry = next(iter(gateway.cache.entries.items()))
    gateway.cache.entries[key] = replace(entry, expires_at=time.monotonic() + 10.5)
    response = await gateway.proxy(proxy_request(gateway, tail=path))
    assert response.headers["X-Net-Cache"] == "HIT"
    assert 0 < int(response.headers["Cache-Control"].split("=")[1]) <= 10
    gateway.client.session.request.assert_called_once()


@pytest.mark.parametrize(
    ("tail", "mime", "query", "vary"),
    [
        ("maimai-mobile/img/avatar.png", "image/png", "user=123", ""),
        ("maimai-mobile/js/account.js", "application/javascript", "token=secret", ""),
        ("maimai-mobile/css/common.css", "application/json", "", ""),
        ("maimai-mobile/img/avatar.png", "text/html", "", ""),
        ("maimai-mobile/css/common.css", "image/png", "", ""),
        ("maimai-mobile/home/", "text/css", "", ""),
        ("maimai-mobile/img/avatar.png", "image/png", "", "Cookie"),
        ("maimai-mobile/img/avatar.png", "image/png", "", "Accept, *"),
        ("maimai-mobile/img/avatar.png", "image/png", "", "Authorization"),
    ],
)
async def test_long_cache_excludes_dynamic_queries_mime_mismatches_and_personalized_responses(
    gateway, tail, mime, query, vary
):
    gateway.client.session.request.return_value = Upstream(
        b"dynamic-content", headers={"Content-Type": mime, "Vary": vary}
    )
    for _ in range(2):
        request = proxy_request(gateway, tail=tail)
        request.rel_url = request.rel_url.with_query(query)
        response = await gateway.proxy(request)
        assert response.headers["Cache-Control"] == "private, no-store"
    assert not gateway.cache.entries
    assert gateway.client.session.request.call_count == 2


async def test_default_backend_cache_lives_seven_days_and_keeps_versioned_static_resources(gateway):
    assert gateway.settings.webapp_cache_ttl == 604800
    gateway.client.session.request.return_value = Upstream(
        b"image", headers={"Content-Type": "image/png"}
    )
    request = proxy_request(gateway, tail="maimai-mobile/img/title.png")
    request.rel_url = request.rel_url.with_query(ver="2026.11")
    await gateway.proxy(request)
    entry = next(iter(gateway.cache.entries.values()))
    assert 604798 < entry.expires_at - time.monotonic() <= 604800
    assert (await gateway.proxy(request)).headers["X-Net-Cache"] == "HIT"
    gateway.client.session.request.assert_called_once()


@pytest.mark.parametrize(
    "location",
    [
        GAME_ORIGIN + "/maimai-mobile/record/",
        PROXY_PATH + "/maimai-mobile/record/",
        "/maimai-mobile/record/",
    ],
)
async def test_redirects_remain_inside_authenticated_session(gateway, location):
    gateway.client.session.request.return_value = Upstream(b"", 302, {"Location": location})
    browser_request = proxy_request(gateway)
    response = await gateway.proxy(browser_request)
    assert response.status == 302
    assert (
        response.headers["Location"]
        == f"/net/p/{browser_request.match_info['sid']}/maimai-mobile/record/"
    )


async def test_external_redirect_is_not_followed_or_returned(gateway):
    gateway.client.session.request.return_value = Upstream(
        b"", 302, {"Location": "https://other.example/private?secret=token"}
    )
    response = await gateway.proxy(proxy_request(gateway))
    assert response.status == 502
    assert "other.example" not in response.text and "secret=token" not in response.text


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_does_not_return_upstream_private_body(gateway, status):
    gateway.client.session.request.return_value = Upstream(b"private-token", status)
    response = await gateway.proxy(proxy_request(gateway))
    assert response.status == 502
    assert "private-token" not in response.text
    assert "/token" in response.text


@pytest.mark.parametrize(
    ("error", "status"),
    [(TimeoutError(), 504), (aiohttp.ClientConnectionError("private-url"), 502)],
)
async def test_network_failure_returns_safe_error(gateway, error, status):
    gateway.client.session.request.side_effect = error
    response = await gateway.proxy(proxy_request(gateway))
    assert response.status == status
    assert "private-url" not in response.text


async def test_proxy_limits_response_size(gateway, monkeypatch):
    monkeypatch.setattr("wahlap_bot.webapp.MAX_PROXY_BYTES", 3)
    response = await gateway.proxy(proxy_request(gateway))
    assert response.status == 502
    assert "过大" in response.text


async def test_routes_bootstrap_and_shutdown_without_prefetching(gateway):
    app = gateway.application()
    for method, path, handler in (
        ("GET", "/net/", gateway.bootstrap),
        ("POST", "/net/session", gateway.create_session),
        ("GET", "/net/p/session/maimai-mobile/home/", gateway.proxy),
        ("POST", "/net/p/session/maimai-mobile/submit/", gateway.proxy),
    ):
        match = await app.router.resolve(make_mocked_request(method, path))
        assert match.handler == handler
    response = await gateway.bootstrap(None)
    assert "<title>maimai DX NET</title>" in response.text
    assert '"/net/session"' in response.text
    assert "app.initData" in response.text and "iframe" in response.text
    assert "正在打开" not in response.text
    assert FIRST.auth_token not in response.text and FIRST.endpoint not in response.text
    gateway.client.session.request.assert_not_called()
    gateway.sessions.create(FIRST)
    await gateway.close()
    assert not gateway.sessions.sessions


async def test_quick_gateway_refuses_authentication_until_real_origin_is_known(gateway):
    quick = NetGateway(
        replace(gateway.settings, webapp_public_url=None, cloudflared_mode="quick"),
        gateway.store,
        gateway.client,
    )
    browser_request = request(data={"init_data": signed_data()})
    assert (await quick.create_session(browser_request)).status == 503
    assert quick.origin is None and not quick.sessions.sessions
    quick.set_public_url("https://random.trycloudflare.com/net/")
    # The original origin must stop being accepted once the real origin is set.
    assert (await quick.create_session(browser_request)).status == 403
    browser_request.headers["Origin"] = "https://random.trycloudflare.com"
    assert (await quick.create_session(browser_request)).status == 200
    with pytest.raises(ValueError, match="路径"):
        quick.set_public_url("https://random.trycloudflare.com/other/")
    gateway.client.session.request.assert_not_called()
    await quick.close()
