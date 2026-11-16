# 舞萌 Telegram Bot

通过 [maiserver](https://github.com/Redbeanw44602/maiserver) 在 Telegram 私聊中获取玩家二维码、打开 maimai DX NET。支持多用户，每人独立设置自己的 endpoint 和认证 Token。

## 安装与启动

准备 Python 3.11+、[@BotFather](https://t.me/BotFather) 提供的 Bot Token，以及已登录微信的 maiserver 实例。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

要直接在主菜单打开 NET 小程序，先[安装 cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)，然后在 `.env` 填写：

```dotenv
TELEGRAM_BOT_TOKEN=你的机器人Token
CLOUDFLARED_MODE=quick
WEBAPP_PUBLIC_URL=
```

启动：

```sh
python -m wahlap_bot
```

机器人会自动启动 cloudflared 并获取临时 HTTPS 地址。临时地址会随重启更换，重启后发送 `/start` 获取新菜单。

## 使用

1. 私聊机器人，发送 `/start`。
2. 填写自己的 maiserver 地址，例如 `http://127.0.0.1:8080`。
3. 按提示填写 `X-Auth-Token`；无认证的服务选择「无需 Token」。
4. 点击「玩家二维码」或「打开 maimai DX NET」。

`127.0.0.1` 指机器人所在机器。其他用户需提供机器人能访问的 maiserver 地址；每个实例对应其登录的微信账号。

| 命令 | 功能 |
| --- | --- |
| `/start` | 首次设置 / 主菜单 |
| `/qrcode` | 获取玩家二维码 |
| `/net` | 打开 NET 小程序入口 |
| `/settings` | 查看和修改账号设置 |
| `/reset` | 删除自己的配置并重新设置 |
| `/help` | 完整命令说明 |

## 固定 HTTPS 地址（可选）

**Cloudflare Named Tunnel**：在 Cloudflare 后台创建隧道，将公开域名的服务地址设为 `http://127.0.0.1:8081`，然后配置：

```dotenv
CLOUDFLARED_MODE=named
CLOUDFLARED_TUNNEL_TOKEN=你的TunnelToken
WEBAPP_PUBLIC_URL=https://bot.example.com/net/
```

**已有 HTTPS 反向代理或独立隧道**：配置：

```dotenv
CLOUDFLARED_MODE=off
WEBAPP_PUBLIC_URL=https://bot.example.com/net/
```

将 `/net/` 及其子路径原样转发到 `http://127.0.0.1:8081`，可参考 [Nginx 示例](deploy/nginx-net.conf.example)。

如果暂不配置 HTTPS 入口，可使用默认配置启动；NET 会通过先获取链接、再点击打开按钮的方式访问。

其他设置（Telegram 代理、使用者限制、监听端口、缓存等）见 [.env.example](.env.example)。
