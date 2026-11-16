"""Own a cloudflared child process without passing secrets in its command line."""

import asyncio
import logging
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from .config import Settings, normalize_webapp_url

logger = logging.getLogger(__name__)
PROCESS_EXIT_TIMEOUT = 5
QUICK_URL = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)(?![a-z0-9.-])")
RUNTIME_ENV = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}


class CloudflaredError(Exception):
    """A safe startup error; never contains the child's raw output or a token."""


def local_origin(settings: Settings) -> str:
    host = settings.webapp_listen_host
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.webapp_listen_port}"


class CloudflaredTunnel:
    def __init__(self, settings: Settings):
        if settings.cloudflared_mode == "off":
            raise ValueError("cloudflared is disabled")
        self.settings = settings
        self.public_url: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._ready: asyncio.Future[str] | None = None
        self._config_dir: tempfile.TemporaryDirectory | None = None
        self._closing = False

    @property
    def running(self) -> bool:
        return (
            not self._closing
            and self.public_url is not None
            and self.process is not None
            and self.process.returncode is None
            and self._reader is not None
            and not self._reader.done()
        )

    async def start(self) -> str:
        if self.process is not None:
            raise RuntimeError("cloudflared has already been started")
        self._closing = False
        self._ready = asyncio.get_running_loop().create_future()
        try:
            # Ignore ~/.cloudflared/config.yml and inherited TUNNEL_* variables:
            # a user's unrelated tunnel must not change what gets published.
            self._config_dir = tempfile.TemporaryDirectory(prefix="wahlap-cloudflared-")
            config_path = Path(self._config_dir.name) / "config.yml"
            config_path.write_text("{}\n", encoding="utf-8")
            command = [
                self.settings.cloudflared_bin,
                "tunnel",
                "--config",
                str(config_path),
                "--no-autoupdate",
                "--loglevel",
                "info",
                "--metrics",
                "127.0.0.1:0",
                "--protocol",
                self.settings.cloudflared_protocol,
            ]
            tunnel_env = {key: value for key, value in os.environ.items() if key in RUNTIME_ENV}
            if self.settings.cloudflared_mode == "quick":
                command.extend(["--url", local_origin(self.settings)])
            else:
                command.append("run")
                tunnel_env["TUNNEL_TOKEN"] = self.settings.cloudflared_tunnel_token
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=tunnel_env,
            )
            self._reader = asyncio.create_task(self._read_output(), name="cloudflared-output")
            url = await asyncio.wait_for(
                asyncio.shield(self._ready), self.settings.cloudflared_startup_timeout
            )
            if not self.running:
                raise CloudflaredError("cloudflared 在建立连接后退出，请检查隧道配置。")
            logger.info("cloudflared 隧道已连接（%s 模式）。", self.settings.cloudflared_mode)
            return url
        except FileNotFoundError:
            await self.close()
            raise CloudflaredError(
                "未找到 cloudflared，请先安装，或通过 CLOUDFLARED_BIN 设置可执行文件路径。"
            ) from None
        except TimeoutError:
            await self.close()
            raise CloudflaredError(
                "等待 cloudflared 连接超时，请检查网络、Tunnel Token 和启动超时配置。"
            ) from None
        except OSError:
            await self.close()
            raise CloudflaredError("无法启动 cloudflared，请检查可执行文件及运行权限。") from None
        except BaseException:
            await self.close()
            raise

    async def _read_output(self) -> None:
        origin = None
        connected = False
        try:
            while line := await self.process.stdout.readline():
                # Handle both cloudflared's text logs and JSON-formatted logs.
                # Raw output is deliberately neither logged nor retained.
                value = line.decode("utf-8", errors="replace")
                if self.settings.cloudflared_mode == "quick" and origin is None:
                    for match in QUICK_URL.finditer(value):
                        if match[1] not in {"api.trycloudflare.com", "www.trycloudflare.com"}:
                            origin = "https://" + match[1]
                            break
                connected |= "Registered tunnel connection" in value
                if connected and not self._ready.done():
                    url = (
                        (origin + "/net/" if origin else None)
                        if self.settings.cloudflared_mode == "quick"
                        else self.settings.webapp_public_url
                    )
                    if url:
                        self.public_url = normalize_webapp_url(url)
                        self._ready.set_result(self.public_url)
            await self.process.wait()
        except (OSError, ValueError):
            pass
        finally:
            if not self._closing:
                if not self._ready.done():
                    self._ready.set_exception(
                        CloudflaredError(
                            "cloudflared 未能建立隧道，"
                            "请检查网络、Tunnel Token 和 cloudflared 版本。"
                        )
                    )
                elif self.public_url:
                    logger.error("cloudflared 已停止，NET 隧道不可用；请重启机器人。")

    async def close(self) -> None:
        self._closing = True
        try:
            if self.process is not None and self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), PROCESS_EXIT_TIMEOUT)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
        finally:
            if self._reader is not None:
                if not self._reader.done():
                    self._reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reader
                self._reader = None
            if self._ready is not None:
                if not self._ready.done():
                    self._ready.cancel()
                elif not self._ready.cancelled():
                    self._ready.exception()
            if self._config_dir is not None:
                self._config_dir.cleanup()
                self._config_dir = None
            self.public_url = None
