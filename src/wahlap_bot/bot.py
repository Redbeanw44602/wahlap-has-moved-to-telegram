"""Private-chat onboarding, account settings, and Telegram feature handlers."""

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    ExtBot,
    MessageHandler,
    TypeHandler,
    filters,
)

from .concurrency import PerUserUpdateProcessor
from .config import Settings
from .endpoints import UserEndpoint, normalize_endpoint, validate_auth_token
from .maiserver import MaiserverClient, MaiserverError, create_api_session
from .messages import clear_buttons, same_message, show_message, show_photo
from .profile import BotCommandsSync
from .storage import UserStore
from .tunnel import CloudflaredTunnel
from .webapp import NetGateway

logger = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
DRAFT = "endpoint_draft"
BOT_COMMANDS = (
    BotCommand("start", "设置账号 / 主菜单"),
    BotCommand("qrcode", "获取玩家二维码"),
    BotCommand("net", "打开 maimai DX NET"),
    BotCommand("endpoint", "修改 API endpoint"),
    BotCommand("token", "修改或清除认证 Token"),
    BotCommand("settings", "查看当前设置"),
    BotCommand("cancel", "取消设置"),
    BotCommand("reset", "删除自己的配置"),
    BotCommand("help", "使用帮助"),
)


def menu(webapp_url: str | None = None) -> InlineKeyboardMarkup:
    net_button = (
        InlineKeyboardButton("打开 maimai DX NET", web_app=WebAppInfo(webapp_url))
        if webapp_url
        else InlineKeyboardButton("打开 maimai DX NET", callback_data="net")
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("玩家二维码", callback_data="qrcode")],
            [net_button],
            [InlineKeyboardButton("账号设置", callback_data="settings")],
        ]
    )


def feature_menu(feature: str) -> InlineKeyboardMarkup:
    label = {"qrcode": "刷新二维码", "net": "重新获取链接"}[feature]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=feature)],
            [InlineKeyboardButton("返回主菜单", callback_data="menu")],
        ]
    )


class BotHandlers:
    def __init__(self, settings: Settings, store: UserStore):
        self.settings = settings
        self.store = store
        self.client: MaiserverClient | None = None
        self.gateway: NetGateway | None = None
        self.tunnel: CloudflaredTunnel | None = None
        self.profile: BotCommandsSync | None = None

    @property
    def webapp_url(self) -> str | None:
        if self.tunnel is not None:
            return self.tunnel.public_url if self.tunnel.running else None
        return self.settings.webapp_public_url

    async def _show_menu(
        self, update: Update, text: str, *, target: Message | None = None
    ) -> Message:
        return await show_message(update, text, target=target, reply_markup=menu(self.webapp_url))

    async def _discard_draft(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data.pop(DRAFT, {})
        prompt = draft.get("prompt")
        # A callback will replace its own message's keyboard with the next screen.
        if not update.callback_query or not same_message(prompt, update.effective_message):
            await clear_buttons(prompt)

    async def guard(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_chat is None:
            raise ApplicationHandlerStop
        if update.edited_message is not None:
            raise ApplicationHandlerStop
        explanation = None
        if update.effective_chat.type != ChatType.PRIVATE:
            explanation = "请私聊机器人使用账号功能。"
        elif (
            self.settings.allowed_user_ids
            and update.effective_user.id not in self.settings.allowed_user_ids
        ):
            explanation = "当前账号没有使用此机器人的权限。"
        if explanation:
            if update.callback_query:
                await update.callback_query.answer(explanation, show_alert=True)
            elif update.effective_message and (update.effective_message.text or "").startswith("/"):
                await update.effective_message.reply_text(explanation)
            raise ApplicationHandlerStop

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._discard_draft(update, context)
        if await self.store.get(update.effective_user.id) is None:
            context.args = []
            await self.endpoint(update, context)
        else:
            await self._show_menu(update, "请选择要使用的功能。")

    async def endpoint(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._discard_draft(update, context)
        context.user_data[DRAFT] = {"stage": "endpoint"}
        if context.args:
            await self._receive_endpoint(update, context, " ".join(context.args))
            return
        context.user_data[DRAFT]["prompt"] = await show_message(
            update,
            "请发送你自己的 maiserver API endpoint。\n"
            "例如：http://127.0.0.1:8080\n\n"
            "地址需要能从机器人服务器访问；127.0.0.1 指机器人所在机器。\n"
            "可用 /cancel 取消设置。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("取消设置", callback_data="cancel")]]
            ),
        )

    async def _receive_endpoint(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
    ) -> None:
        try:
            endpoint = normalize_endpoint(value)
        except ValueError as exc:
            draft = context.user_data[DRAFT]
            draft["prompt"] = await show_message(
                update,
                str(exc),
                target=draft.get("prompt"),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("取消设置", callback_data="cancel")]]
                ),
            )
            return
        context.user_data[DRAFT].update(stage="token", endpoint=endpoint)
        await self._ask_token(update, context)

    async def _ask_token(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, error: str = ""
    ) -> None:
        draft = context.user_data[DRAFT]
        draft["prompt"] = await show_message(
            update,
            (f"{error}\n\n" if error else "")
            + "如果此 endpoint 要求 X-Auth-Token，请发送 Token。\n"
            "本地无认证服务请点击「无需 Token」，或发送 /skip。\n"
            "可用 /cancel 取消，本次设置完成后才会保存。",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("无需 Token", callback_data="skip_token")],
                    [InlineKeyboardButton("取消设置", callback_data="cancel")],
                ]
            ),
            target=draft.get("prompt"),
        )

    async def token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_account(update, context)
        if user is None:
            return
        await self._discard_draft(update, context)
        context.user_data[DRAFT] = {"stage": "token", "endpoint": user.endpoint}
        await self._ask_token(update, context)

    async def skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data.get(DRAFT, {})
        if draft.get("stage") != "token":
            await show_message(update, "没有待设置的 Token，请先用 /endpoint 设置地址。")
            return
        await self._save(update, context, None)

    async def _save(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, token: str | None
    ) -> None:
        draft = context.user_data[DRAFT]
        await self.store.save(UserEndpoint(update.effective_user.id, draft["endpoint"], token))
        if self.gateway is not None:
            self.gateway.revoke_user(update.effective_user.id)
        context.user_data.pop(DRAFT, None)
        await self._show_menu(
            update,
            "配置已保存。请选择要使用的功能。",
            target=draft.get("prompt"),
        )

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data.get(DRAFT, {})
        if draft.get("stage") == "endpoint":
            await self._receive_endpoint(update, context, update.effective_message.text)
        elif draft.get("stage") == "token":
            value = update.effective_message.text
            with suppress(TelegramError):
                await update.effective_message.delete()
            try:
                token = validate_auth_token(value)
            except ValueError as exc:
                await self._ask_token(update, context, error=str(exc))
                return
            await self._save(update, context, token)
        elif await self.store.get(update.effective_user.id) is None:
            await self.endpoint(update, context)
        else:
            await self._show_menu(update, "请点击下方功能，或发送 /help 查看命令。")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data.pop(DRAFT, {})
        configured = await self.store.get(update.effective_user.id)
        if configured:
            await self._show_menu(update, "已取消设置，原配置已保留。", target=draft.get("prompt"))
        else:
            await show_message(
                update,
                "已取消设置。发送 /start 可重新设置。",
                target=draft.get("prompt"),
            )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.store.delete(update.effective_user.id)
        if self.gateway is not None:
            self.gateway.revoke_user(update.effective_user.id)
        await self._discard_draft(update, context)
        await show_message(update, "已删除你的 endpoint 和 Token。")
        await self.endpoint(update, context)

    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_account(update, context)
        if user is None:
            return
        await show_message(
            update,
            f"API endpoint：{user.endpoint}\n"
            f"认证 Token：{'已设置' if user.auth_token else '未设置'}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("修改 endpoint", callback_data="endpoint")],
                    [InlineKeyboardButton("修改 / 清除 Token", callback_data="token")],
                    [InlineKeyboardButton("返回主菜单", callback_data="menu")],
                ]
            ),
        )

    async def help(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_menu(
            update,
            "/start — 设置账号 / 主菜单\n"
            "/qrcode — 获取玩家二维码\n"
            "/net — 打开 maimai DX NET\n"
            "/endpoint — 修改 API endpoint\n"
            "/token — 修改或清除认证 Token\n"
            "/settings — 查看设置\n"
            "/cancel — 取消尚未保存的设置\n"
            "/reset — 删除自己的配置并重新设置\n\n"
            "每人需使用绑定自己微信账号的 maiserver 实例。",
        )

    async def _require_account(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> UserEndpoint | None:
        user = await self.store.get(update.effective_user.id)
        if user is None:
            context.args = []
            await self.endpoint(update, context)
        return user

    async def qrcode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_account(update, context)
        if user is None:
            return
        status = await show_message(update, "正在获取新的玩家二维码，请稍候……")
        try:
            qr = await self.client.get_qrcode(user)
            image = await asyncio.to_thread(qr.png)
            expiry = datetime.fromtimestamp(qr.expires_at, BEIJING).strftime("%m-%d %H:%M:%S")
            caption = f"玩家二维码\n有效期至：{expiry}（北京时间）"
            if qr.description:
                caption += f"\n\n{qr.description[:400]}"
            with image:
                await show_photo(
                    status,
                    image,
                    caption,
                    feature_menu("qrcode"),
                )
        except MaiserverError as exc:
            await show_message(update, str(exc), target=status, reply_markup=feature_menu("qrcode"))
            return
        except Exception as exc:
            await self._unexpected_error(update, exc, target=status)

    async def net(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self._require_account(update, context)
        if user is None:
            return
        if self.tunnel is not None and not self.tunnel.running:
            await show_message(
                update,
                "NET 的 HTTPS 隧道暂时不可用，请联系机器人运行者重启服务。",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("重试", callback_data="net")],
                        [InlineKeyboardButton("返回主菜单", callback_data="menu")],
                    ]
                ),
            )
            return
        if self.webapp_url:
            await show_message(
                update,
                "点击下方按钮在 Telegram 内打开 maimai DX NET。",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "打开 maimai DX NET",
                                web_app=WebAppInfo(self.webapp_url),
                            )
                        ],
                        [InlineKeyboardButton("返回主菜单", callback_data="menu")],
                    ]
                ),
            )
            return
        status = await show_message(update, "正在获取 maimai DX NET 授权链接，请稍候……")
        try:
            url = await self.client.get_net_url(user)
            await show_message(
                update,
                "点击下方按钮在 Telegram 内打开 maimai DX NET。\n"
                "链接失效时请重新获取；开启新会话后，旧会话可能失效。",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("打开 maimai DX NET", web_app=WebAppInfo(url))],
                        [InlineKeyboardButton("重新获取链接", callback_data="net")],
                        [InlineKeyboardButton("返回主菜单", callback_data="menu")],
                    ]
                ),
                target=status,
            )
        except MaiserverError as exc:
            await show_message(update, str(exc), target=status, reply_markup=feature_menu("net"))
            return
        except Exception as exc:
            await self._unexpected_error(update, exc, target=status)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query.data in {"skip_token", "cancel"}:
            draft = context.user_data.get(DRAFT, {})
            if not same_message(draft.get("prompt"), update.effective_message) or (
                update.callback_query.data == "skip_token" and draft.get("stage") != "token"
            ):
                # A delayed repeat must not consume a newer draft or strip the
                # replacement menu from a message that has already been updated.
                await update.callback_query.answer("这次设置已结束，请使用当前提示。")
                return
        await update.callback_query.answer()
        # A callback update has no command arguments; prevent stale arguments from
        # being interpreted as a new endpoint when moving between menu screens.
        context.args = []
        if update.callback_query.data in {"qrcode", "net", "settings"}:
            await self._discard_draft(update, context)
        callbacks = {
            "qrcode": self.qrcode,
            "net": self.net,
            "settings": self.show_settings,
            "endpoint": self.endpoint,
            "token": self.token,
            "skip_token": self.skip,
            "cancel": self.cancel,
            "menu": self.start,
        }
        handler = callbacks.get(update.callback_query.data, self.start)
        await handler(update, context)

    async def _unexpected_error(
        self, update: object, error: BaseException, *, target: Message | None = None
    ) -> None:
        # Exception messages/tracebacks can include request URLs or Telegram tokens.
        logger.error("处理请求失败：%s", type(error).__name__)
        if isinstance(update, Update) and update.effective_message:
            with suppress(TelegramError):
                await show_message(
                    update,
                    "操作暂时失败，请稍后重试。",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("返回主菜单", callback_data="menu")]]
                    ),
                    target=target,
                )

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._unexpected_error(update, context.error)


def create_application(settings: Settings, *, bot: ExtBot | None = None) -> Application:
    store = UserStore(settings.database_path)
    handlers = BotHandlers(settings, store)

    async def stop_profile(_application: Application) -> None:
        if handlers.profile is not None:
            await handlers.profile.close()

    async def shutdown(_application: Application) -> None:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(store.close)
            if handlers.client is not None:
                cleanup.push_async_callback(handlers.client.session.close)
            if handlers.gateway is not None:
                cleanup.push_async_callback(handlers.gateway.close)
            if handlers.tunnel is not None:
                cleanup.push_async_callback(handlers.tunnel.close)
            cleanup.push_async_callback(stop_profile, _application)

    async def initialize(application: Application) -> None:
        try:
            await store.open()
            handlers.client = MaiserverClient(create_api_session(), settings.api_timeout)
            if settings.webapp_public_url or settings.cloudflared_mode != "off":
                handlers.gateway = NetGateway(settings, store, handlers.client)
                await handlers.gateway.start()
            if settings.cloudflared_mode != "off":
                handlers.tunnel = CloudflaredTunnel(settings)
                public_url = await handlers.tunnel.start()
                handlers.gateway.set_public_url(public_url)
            handlers.profile = BotCommandsSync(application.bot, BOT_COMMANDS)
            await handlers.profile.start()
        except BaseException:
            await shutdown(application)
            raise

    builder = (
        Application.builder()
        .concurrent_updates(PerUserUpdateProcessor(32))
        .post_init(initialize)
        .post_stop(stop_profile)
        .post_shutdown(shutdown)
    )
    if bot is not None:
        builder.bot(bot)
    else:
        builder.token(settings.bot_token).defaults(
            Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
        )
        if settings.telegram_proxy:
            builder.proxy(settings.telegram_proxy).get_updates_proxy(settings.telegram_proxy)
    application = builder.build()
    application.bot_data["handlers"] = handlers
    application.add_handler(TypeHandler(Update, handlers.guard), group=-1)
    for name, handler in {
        "start": handlers.start,
        "help": handlers.help,
        "endpoint": handlers.endpoint,
        "token": handlers.token,
        "skip": handlers.skip,
        "cancel": handlers.cancel,
        "reset": handlers.reset,
        "settings": handlers.show_settings,
        "qrcode": handlers.qrcode,
        "net": handlers.net,
    }.items():
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(CallbackQueryHandler(handlers.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text))
    application.add_error_handler(handlers.error)
    return application
