"""Normalize user supplied API base URLs without discarding reverse proxy prefixes."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


def normalize_endpoint(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("请输入完整的 HTTP(S) 地址，例如 http://127.0.0.1:8080。")
    if "\\" in value or any(c in value for c in "?#"):
        raise ValueError("Endpoint 不能包含反斜杠、查询参数或 # 片段。")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ValueError("Endpoint 不能包含用户名或密码；认证请使用单独的 Token 设置。")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        if any(segment in {".", ".."} for segment in parts.path.split("/")):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Endpoint 无效；请使用 http(s)://主机:端口[/前缀]。") from exc
    path = parts.path.rstrip("/")
    # Accept both the server root and the API base as commonly supplied by users.
    if path.endswith("/api/v1"):
        path = path[:-7]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def validate_auth_token(value: str) -> str:
    token = value.strip()
    if not token or len(token) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Token 必须是 1–2048 个不含空白的 ASCII 字符。")
    return token


@dataclass(frozen=True, slots=True)
class UserEndpoint:
    user_id: int
    endpoint: str
    auth_token: str | None = field(default=None, repr=False)

    def api_url(self, path: str) -> str:
        return f"{self.endpoint}/api/v1/{path.lstrip('/')}"

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.auth_token} if self.auth_token else {}
