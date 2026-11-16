import pytest

from wahlap_bot.config import Settings, normalize_webapp_url


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setenv("ALLOWED_USER_IDS", "101, 202")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "socks5://127.0.0.1:7890")
    settings = Settings.from_env()
    assert settings.allowed_user_ids == frozenset({101, 202})
    assert settings.telegram_proxy == "socks5://127.0.0.1:7890"
    assert "secret" not in repr(settings)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "invalid"])
def test_bad_timeout(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("API_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="API_TIMEOUT_SECONDS"):
        Settings.from_env()


def test_missing_bot_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://bot.example.com", "https://bot.example.com/net/"),
        ("https://BOT.example.com:443/net", "https://bot.example.com/net/"),
        ("https://bot.example.com:8443/app/net/", "https://bot.example.com:8443/app/net/"),
        ("https://[::1]:8443/net/", "https://[::1]:8443/net/"),
        ("https://舞萌.example/net/", "https://xn--cd1an3b.example/net/"),
    ],
)
def test_webapp_public_url_normalizes_prefix_and_browser_origin(value, expected):
    assert normalize_webapp_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://bot.example.com/net/",
        "https://user:secret@bot.example.com/net/",
        "https://bot.example.com/net/?token=secret",
        "https://bot.example.com/net/#secret",
        "https://bot.example.com/net/../api/",
        "https://bot.example.com/net/%2e%2e/",
        "https://bot.example.com/net/{tail}/",
        "https://bot.example.com:0/net/",
        "https://bot.example.com:65536/net/",
        "https://bot.example.com/net\\other",
        "https://bad host.example/net/",
    ],
)
def test_invalid_webapp_public_url_is_rejected(value):
    with pytest.raises(ValueError, match="WEBAPP_PUBLIC_URL"):
        normalize_webapp_url(value)


def test_webapp_gateway_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("WEBAPP_PUBLIC_URL", "https://BOT.example.com:443/app")
    monkeypatch.setenv("WEBAPP_LISTEN_HOST", "0.0.0.0")
    monkeypatch.setenv("WEBAPP_LISTEN_PORT", "9090")
    settings = Settings.from_env()
    assert settings.webapp_public_url == "https://bot.example.com/app/"
    assert settings.webapp_listen_host == "0.0.0.0"
    assert settings.webapp_listen_port == 9090


@pytest.mark.parametrize("value", ["0", "65536", "invalid"])
def test_invalid_webapp_listen_port(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("WEBAPP_LISTEN_PORT", value)
    with pytest.raises(ValueError, match="WEBAPP_LISTEN_PORT"):
        Settings.from_env()


def test_quick_tunnel_configuration(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.delenv("WEBAPP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARED_TUNNEL_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARED_MODE", " QUICK ")
    monkeypatch.setenv("CLOUDFLARED_BIN", "/opt/bin/cloudflared")
    monkeypatch.setenv("CLOUDFLARED_STARTUP_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("CLOUDFLARED_PROTOCOL", "HTTP2")
    settings = Settings.from_env()
    assert settings.cloudflared_mode == "quick"
    assert settings.webapp_public_url is None
    assert settings.cloudflared_bin == "/opt/bin/cloudflared"
    assert settings.cloudflared_startup_timeout == 90
    assert settings.cloudflared_protocol == "http2"


def test_named_tunnel_configuration_keeps_token_private(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-secret")
    monkeypatch.setenv("CLOUDFLARED_MODE", "named")
    monkeypatch.setenv("WEBAPP_PUBLIC_URL", "https://BOT.example.com:443/net")
    monkeypatch.setenv("CLOUDFLARED_TUNNEL_TOKEN", "tunnel-secret")
    settings = Settings.from_env()
    assert settings.webapp_public_url == "https://bot.example.com/net/"
    assert settings.cloudflared_tunnel_token == "tunnel-secret"
    assert "tunnel-secret" not in repr(settings) and "bot-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cloudflared_mode": "bad"}, "CLOUDFLARED_MODE"),
        (
            {"cloudflared_mode": "quick", "webapp_public_url": "https://example.com/net/"},
            "WEBAPP_PUBLIC_URL",
        ),
        ({"cloudflared_mode": "quick", "cloudflared_tunnel_token": "secret"}, "named"),
        (
            {"cloudflared_mode": "named", "webapp_public_url": "https://example.com/net/"},
            "CLOUDFLARED_TUNNEL_TOKEN",
        ),
        ({"cloudflared_mode": "named", "cloudflared_tunnel_token": "secret"}, "WEBAPP_PUBLIC_URL"),
        ({"cloudflared_mode": "quick", "cloudflared_bin": ""}, "CLOUDFLARED_BIN"),
        ({"cloudflared_protocol": "bad"}, "CLOUDFLARED_PROTOCOL"),
    ],
)
def test_inconsistent_cloudflared_configuration_is_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(bot_token="bot", **overrides)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "bad"])
def test_invalid_cloudflared_startup_timeout(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("CLOUDFLARED_STARTUP_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="CLOUDFLARED_STARTUP_TIMEOUT_SECONDS"):
        Settings.from_env()


def test_net_cache_configuration(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("WEBAPP_CACHE_MAX_MB", "32")
    monkeypatch.setenv("WEBAPP_CACHE_TTL_SECONDS", "600")
    settings = Settings.from_env()
    assert settings.webapp_cache_max_mb == 32
    assert settings.webapp_cache_ttl == 600


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEBAPP_CACHE_MAX_MB", "-1"),
        ("WEBAPP_CACHE_MAX_MB", "bad"),
        ("WEBAPP_CACHE_TTL_SECONDS", "-1"),
        ("WEBAPP_CACHE_TTL_SECONDS", "nan"),
        ("WEBAPP_CACHE_TTL_SECONDS", "bad"),
    ],
)
def test_invalid_cache_settings(monkeypatch, name, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()
