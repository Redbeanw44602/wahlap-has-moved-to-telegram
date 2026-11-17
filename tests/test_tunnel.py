import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wahlap_bot.config import Settings
from wahlap_bot.tunnel import CloudflaredError, CloudflaredTunnel, local_origin

PUBLIC_URL = "https://example-random-words.trycloudflare.com/net/"
NAMED_URL = "https://bot.example.com/app/net/"
TUNNEL_TOKEN = "private-tunnel-token"
URL_LINE = f"INF | {PUBLIC_URL.removesuffix('/net/')} |"
READY_LINE = '{"level":"info","message":"Registered tunnel connection","connIndex":0}'


class TunnelProcess:
    def __init__(self, *, stops_on_terminate=True):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.exited = asyncio.Event()
        self.stops_on_terminate = stops_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0

    def output(self, *lines):
        for line in lines:
            self.stdout.feed_data((line + "\n").encode())

    def exit(self, code=0):
        self.returncode = code
        self.stdout.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.stops_on_terminate:
            self.exit(-15)

    def kill(self):
        self.kill_calls += 1
        self.exit(-9)


@pytest.fixture
def spawn(monkeypatch):
    process = TunnelProcess()
    launched = asyncio.Event()

    async def run(*_args, **_kwargs):
        launched.set()
        return process

    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr("wahlap_bot.tunnel.asyncio.create_subprocess_exec", mock)
    return process, mock, launched


def quick_settings(**kwargs):
    return Settings(bot_token="private-bot-token", cloudflared_mode="quick", **kwargs)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        ("::", "[::1]"),
        ("::1", "[::1]"),
        ("localhost", "localhost"),
    ],
)
def test_local_origin_uses_gateway_port_and_connectable_host(host, expected):
    assert (
        local_origin(quick_settings(webapp_listen_host=host, webapp_listen_port=9090))
        == f"http://{expected}:9090"
    )


@pytest.mark.parametrize("first", ["url", "connection"])
async def test_quick_waits_for_both_hostname_and_connection(spawn, first):
    process, command, launched = spawn
    tunnel = CloudflaredTunnel(quick_settings())
    process.output(URL_LINE if first == "url" else READY_LINE)
    task = asyncio.create_task(tunnel.start())
    try:
        await launched.wait()
        await asyncio.sleep(0)
        assert not task.done() and not tunnel.running
        process.output(READY_LINE if first == "url" else URL_LINE)
        assert await task == PUBLIC_URL
        assert tunnel.public_url == PUBLIC_URL and tunnel.running
        args, kwargs = command.call_args
        assert args[0:2] == ("cloudflared", "tunnel")
        assert args[args.index("--url") + 1] == "http://127.0.0.1:8081"
        assert args[args.index("--metrics") + 1] == "127.0.0.1:0"
        assert "--no-autoupdate" in args
        assert "TUNNEL_TOKEN" not in kwargs["env"]
        assert "TELEGRAM_BOT_TOKEN" not in kwargs["env"]
        config = Path(args[args.index("--config") + 1])
        assert await asyncio.to_thread(config.read_text) == "{}\n"
        assert kwargs["stderr"] == asyncio.subprocess.STDOUT
        await tunnel.close()
        assert not await asyncio.to_thread(config.exists)
        assert process.returncode == -15
        assert not tunnel.running
        await tunnel.close()
        assert process.terminate_calls == 1
    finally:
        await tunnel.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_named_token_stays_out_of_arguments_and_unrelated_config_is_not_inherited(
    spawn, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "do-not-inherit-bot-token")
    monkeypatch.setenv("TUNNEL_CONFIG", "/unrelated/config.yml")
    monkeypatch.setenv("TUNNEL_TOKEN", "unrelated-token")
    monkeypatch.setenv("TUNNEL_LOGLEVEL", "debug")
    monkeypatch.setenv("CLOUDFLARED_TUNNEL_TOKEN", TUNNEL_TOKEN)
    process, command, _ = spawn
    process.output(URL_LINE, READY_LINE)
    tunnel = CloudflaredTunnel(
        Settings(
            bot_token="bot",
            cloudflared_mode="named",
            webapp_public_url=NAMED_URL,
            cloudflared_tunnel_token=TUNNEL_TOKEN,
            cloudflared_protocol="http2",
        )
    )
    try:
        assert await tunnel.start() == NAMED_URL
        args, kwargs = command.call_args
        assert args[-1] == "run" and "--url" not in args
        assert TUNNEL_TOKEN not in str(args)
        assert kwargs["env"]["TUNNEL_TOKEN"] == TUNNEL_TOKEN
        assert (
            set(kwargs["env"])
            & {"TELEGRAM_BOT_TOKEN", "TUNNEL_CONFIG", "TUNNEL_LOGLEVEL", "CLOUDFLARED_TUNNEL_TOKEN"}
            == set()
        )
        assert args[args.index("--protocol") + 1] == "http2"
    finally:
        await tunnel.close()


@pytest.mark.parametrize("missing", ["url", "connection", "both"])
async def test_startup_timeout_reaps_child_and_cleans_configuration(spawn, missing):
    process, command, _ = spawn
    if missing == "url":
        process.output(READY_LINE)
    elif missing == "connection":
        process.output(URL_LINE)
    tunnel = CloudflaredTunnel(quick_settings(cloudflared_startup_timeout=0.02))
    with pytest.raises(CloudflaredError, match="超时"):
        await tunnel.start()
    assert process.returncode == -15
    assert not tunnel.running and tunnel._reader is None
    args = command.call_args.args
    config = Path(args[args.index("--config") + 1])
    assert not await asyncio.to_thread(config.exists)


@pytest.mark.parametrize(
    "url", ["https://api.trycloudflare.com", "https://fake.trycloudflare.com.evil.example"]
)
async def test_provisioning_host_and_lookalike_domain_are_not_accepted(spawn, url):
    process, _, _ = spawn
    process.output(url, READY_LINE)
    tunnel = CloudflaredTunnel(quick_settings(cloudflared_startup_timeout=0.02))
    with pytest.raises(CloudflaredError, match="超时"):
        await tunnel.start()
    assert tunnel.public_url is None


async def test_child_exit_before_connection_returns_safe_startup_error(spawn, caplog):
    process, _, _ = spawn
    process.output("private-token-in-an-error-response")
    process.exit(1)
    tunnel = CloudflaredTunnel(quick_settings())
    with pytest.raises(CloudflaredError) as exc:
        await tunnel.start()
    assert "private-token" not in str(exc.value) + caplog.text
    assert not tunnel.running and tunnel._config_dir is None


@pytest.mark.parametrize(
    "error", [FileNotFoundError("secret-path"), PermissionError("secret-path")]
)
async def test_missing_or_unusable_binary_has_an_actionable_safe_error(spawn, error):
    _, command, _ = spawn
    command.side_effect = error
    tunnel = CloudflaredTunnel(quick_settings())
    with pytest.raises(CloudflaredError) as exc:
        await tunnel.start()
    assert "cloudflared" in str(exc.value) and "secret-path" not in str(exc.value)
    assert tunnel._config_dir is None


async def test_cancelled_startup_stops_child_and_reader(spawn):
    process, _, launched = spawn
    tunnel = CloudflaredTunnel(quick_settings())
    task = asyncio.create_task(tunnel.start())
    await launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == -15
    assert tunnel._reader is None and tunnel._config_dir is None


async def test_shutdown_kills_child_that_ignores_termination(spawn, monkeypatch):
    monkeypatch.setattr("wahlap_bot.tunnel.PROCESS_EXIT_TIMEOUT", 0.02)
    process, _, _ = spawn
    process.stops_on_terminate = False
    process.output(URL_LINE, READY_LINE)
    tunnel = CloudflaredTunnel(quick_settings())
    await tunnel.start()
    await tunnel.close()
    assert process.terminate_calls == 1 and process.kill_calls == 1
    assert process.returncode == -9


async def test_unexpected_child_exit_marks_tunnel_unavailable_without_leaking_logs(spawn, caplog):
    process, _, _ = spawn
    process.output(URL_LINE, READY_LINE)
    tunnel = CloudflaredTunnel(quick_settings())
    try:
        await tunnel.start()
        process.output("private-token-in-output")
        process.exit(1)
        await tunnel._reader
        assert not tunnel.running
        assert "NET tunnel unavailable" in caplog.text
        assert "private-token" not in caplog.text
    finally:
        await tunnel.close()


@pytest.mark.skipif(os.name != "posix", reason="The executable fixture uses a POSIX shebang")
async def test_real_child_pipe_and_shutdown_without_opening_a_network_tunnel(tmp_path):
    executable = tmp_path / "fake-cloudflared"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"print({URL_LINE!r}, file=sys.stderr, flush=True)\n"
        f"print({READY_LINE!r}, file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    tunnel = CloudflaredTunnel(quick_settings(cloudflared_bin=str(executable)))
    try:
        assert await tunnel.start() == PUBLIC_URL
        assert tunnel.running
    finally:
        await tunnel.close()
    assert tunnel.process.returncode is not None
