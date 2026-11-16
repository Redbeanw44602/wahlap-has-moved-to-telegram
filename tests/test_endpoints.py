import pytest

from wahlap_bot.endpoints import UserEndpoint, normalize_endpoint, validate_auth_token


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" http://127.0.0.1:8080/ ", "http://127.0.0.1:8080"),
        ("https://example.com/api/v1/", "https://example.com"),
        ("https://example.com/mai/api/v1", "https://example.com/mai"),
        ("https://example.com/mai/", "https://example.com/mai"),
        ("http://[::1]:8080", "http://[::1]:8080"),
    ],
)
def test_normalize_endpoint(raw, expected):
    assert normalize_endpoint(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "127.0.0.1:8080",
        "ftp://example.com",
        "http:///missing-host",
        "http://user:password@example.com",
        "http://example.com:invalid",
        "http://example.com:0",
        "http://example.com:99999",
        "http://example.com/?token=secret",
        "http://example.com/#fragment",
        "http://example.com/../private",
        "http://example.com/has space",
        "http://example.com/\x00",
        "http://example.com\\private",
    ],
)
def test_invalid_endpoint(raw):
    with pytest.raises(ValueError):
        normalize_endpoint(raw)


def test_paths_and_auth_are_per_user():
    first = UserEndpoint(1, "https://one.example/mai", "secret-one")
    second = UserEndpoint(2, "http://127.0.0.1:8080")
    assert first.api_url("/auth/qrcode") == "https://one.example/mai/api/v1/auth/qrcode"
    assert first.headers == {"X-Auth-Token": "secret-one"}
    assert second.headers == {}
    assert "secret-one" not in repr(first)


@pytest.mark.parametrize("value", ["", "bad token", "line\r\nbreak", "中文", "x" * 2049])
def test_bad_token(value):
    with pytest.raises(ValueError):
        validate_auth_token(value)


def test_token():
    assert validate_auth_token("  token-_=123  ") == "token-_=123"
