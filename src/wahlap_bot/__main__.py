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
        logging.getLogger(__name__).info("Bot starting")
        application.run_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY], bootstrap_retries=3
        )
        logging.getLogger(__name__).info("Bot stopped")
    except RetryAfter as exc:
        logging.getLogger(__name__).error(
            "Telegram startup rate limited; restart after %.1fs",
            retry_after_seconds(exc) + 0.5,
        )
        raise SystemExit(1) from None
    except CloudflaredError as exc:
        logging.getLogger(__name__).error("Tunnel startup failed error=%s", describe_error(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Bot exited error=%s",
            describe_error(exc),
        )
        raise SystemExit(1) from None
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)


if __name__ == "__main__":
    main()
