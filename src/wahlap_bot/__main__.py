"""Run the bot using Telegram long polling."""

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.error import RetryAfter

from .bot import create_application
from .config import Settings
from .logging_utils import describe_error
from .profile import retry_after_seconds
from .tunnel import CloudflaredError


def main() -> None:
    load_dotenv(".env")
    # Adopt PTB's timedelta format while respecting an explicit legacy setting.
    os.environ.setdefault("PTB_TIMEDELTA", "1")
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTP/Telegram debug output may include URLs, bot tokens, and update contents.
    for name in ("httpx", "httpcore", "telegram", "aiohttp", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    # Python 3.14 no longer creates an implicit main-thread event loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        application = create_application(settings)
        logging.getLogger(__name__).info("机器人启动")
        application.run_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], bootstrap_retries=3
        )
        logging.getLogger(__name__).info("机器人已停止")
    except RetryAfter as exc:
        logging.getLogger(__name__).error(
            "Telegram 启动限流，%.1f 秒后可重启",
            retry_after_seconds(exc) + 0.5,
        )
        raise SystemExit(1) from None
    except CloudflaredError as exc:
        logging.getLogger(__name__).error(
            "隧道启动失败：%s", str(exc).split("，", 1)[0].rstrip("。")
        )
        raise SystemExit(1) from None
    except Exception as exc:
        logging.getLogger(__name__).error(
            "机器人退出：%s",
            describe_error(exc),
        )
        raise SystemExit(1) from None
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
