"""Process configuration; account endpoints are configured by each Telegram user."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def normalize_webapp_url(value: str) -> str:
    value = value.strip()
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
            or not re.fullmatch(r"/[A-Za-z0-9/_~.-]*|", parts.path)
            or any(segment in {".", ".."} for segment in parts.path.split("/"))
        ):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        elif not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            "WEBAPP_PUBLIC_URL 必须是无认证信息、查询参数和片段的 HTTPS 地址，"
            "路径仅支持英文字母、数字、/、_、~、. 和 -。"
        ) from exc
    path = (parts.path.rstrip("/") or "/net") + "/"
    authority = host if port is None or port == 443 else f"{host}:{port}"
    return urlunsplit(("https", authority, path, "", ""))


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str = field(repr=False)
    database_path: Path = Path("data/bot.sqlite3")
    api_timeout: float = 25.0
    telegram_proxy: str | None = field(default=None, repr=False)
    allowed_user_ids: frozenset[int] = frozenset()
    log_level: str = "INFO"
    webapp_public_url: str | None = None
    webapp_listen_host: str = "127.0.0.1"
    webapp_listen_port: int = 8081
    webapp_cache_max_mb: int = 64
    webapp_cache_ttl: float = 604800
    cloudflared_mode: str = "off"
    cloudflared_bin: str = "cloudflared"
    cloudflared_tunnel_token: str | None = field(default=None, repr=False)
    cloudflared_startup_timeout: float = 60.0
    cloudflared_protocol: str = "auto"

    def __post_init__(self) -> None:
        if self.webapp_cache_max_mb < 0:
            raise ValueError("WEBAPP_CACHE_MAX_MB 必须是非负整数。")
        if not math.isfinite(self.webapp_cache_ttl) or self.webapp_cache_ttl < 0:
            raise ValueError("WEBAPP_CACHE_TTL_SECONDS 必须是非负数。")
        if self.cloudflared_mode not in {"off", "quick", "named"}:
            raise ValueError("CLOUDFLARED_MODE 必须是 off、quick 或 named。")
        if (
            not math.isfinite(self.cloudflared_startup_timeout)
            or self.cloudflared_startup_timeout <= 0
        ):
            raise ValueError("CLOUDFLARED_STARTUP_TIMEOUT_SECONDS 必须是正数。")
        if self.cloudflared_protocol not in {"auto", "http2", "quic"}:
            raise ValueError("CLOUDFLARED_PROTOCOL 必须是 auto、http2 或 quic。")
        if self.cloudflared_mode != "off" and not self.cloudflared_bin.strip():
            raise ValueError("CLOUDFLARED_BIN 不能为空。")
        if self.cloudflared_mode == "quick":
            if self.webapp_public_url:
                raise ValueError("Quick Tunnel 会自动获取入口，请将 WEBAPP_PUBLIC_URL 留空。")
            if self.cloudflared_tunnel_token:
                raise ValueError("CLOUDFLARED_TUNNEL_TOKEN 仅用于 named 模式。")
        if self.cloudflared_mode == "named" and (
            not self.webapp_public_url or not self.cloudflared_tunnel_token
        ):
            raise ValueError("named 模式需要 WEBAPP_PUBLIC_URL 和 CLOUDFLARED_TUNNEL_TOKEN。")

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("请在 .env 或环境变量中设置 TELEGRAM_BOT_TOKEN。")
        try:
            timeout = float(os.environ.get("API_TIMEOUT_SECONDS", "25"))
        except ValueError as exc:
            raise ValueError("API_TIMEOUT_SECONDS 必须是正数。") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("API_TIMEOUT_SECONDS 必须是正数。")
        try:
            allowed = frozenset(
                int(value.strip())
                for value in os.environ.get("ALLOWED_USER_IDS", "").split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError("ALLOWED_USER_IDS 必须是以逗号分隔的数字用户 ID。") from exc
        if any(user_id <= 0 for user_id in allowed):
            raise ValueError("ALLOWED_USER_IDS 必须使用正整数用户 ID。")
        level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 无效。")
        public_url = os.environ.get("WEBAPP_PUBLIC_URL", "").strip()
        try:
            webapp_port = int(os.environ.get("WEBAPP_LISTEN_PORT", "8081"))
        except ValueError as exc:
            raise ValueError("WEBAPP_LISTEN_PORT 必须是有效端口号。") from exc
        if not 1 <= webapp_port <= 65535:
            raise ValueError("WEBAPP_LISTEN_PORT 必须是有效端口号。")
        try:
            tunnel_timeout = float(os.environ.get("CLOUDFLARED_STARTUP_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise ValueError("CLOUDFLARED_STARTUP_TIMEOUT_SECONDS 必须是正数。") from exc
        try:
            cache_mb = int(os.environ.get("WEBAPP_CACHE_MAX_MB", "64"))
        except ValueError as exc:
            raise ValueError("WEBAPP_CACHE_MAX_MB 必须是非负整数。") from exc
        try:
            cache_ttl = float(os.environ.get("WEBAPP_CACHE_TTL_SECONDS", "604800"))
        except ValueError as exc:
            raise ValueError("WEBAPP_CACHE_TTL_SECONDS 必须是非负数。") from exc
        return cls(
            bot_token=token,
            database_path=Path(os.environ.get("DATABASE_PATH", "data/bot.sqlite3")),
            api_timeout=timeout,
            telegram_proxy=os.environ.get("TELEGRAM_PROXY_URL", "").strip() or None,
            allowed_user_ids=allowed,
            log_level=level,
            webapp_public_url=normalize_webapp_url(public_url) if public_url else None,
            webapp_listen_host=os.environ.get("WEBAPP_LISTEN_HOST", "127.0.0.1").strip(),
            webapp_listen_port=webapp_port,
            webapp_cache_max_mb=cache_mb,
            webapp_cache_ttl=cache_ttl,
            cloudflared_mode=os.environ.get("CLOUDFLARED_MODE", "off").strip().lower(),
            cloudflared_bin=os.environ.get("CLOUDFLARED_BIN", "cloudflared").strip(),
            cloudflared_tunnel_token=os.environ.get("CLOUDFLARED_TUNNEL_TOKEN", "").strip() or None,
            cloudflared_startup_timeout=tunnel_timeout,
            cloudflared_protocol=os.environ.get("CLOUDFLARED_PROTOCOL", "auto").strip().lower(),
        )
