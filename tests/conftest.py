import itertools
import json
from dataclasses import dataclass
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from telegram import Update
from telegram.ext import ExtBot
from telegram.request import BaseRequest

from wahlap_bot.bot import create_application
from wahlap_bot.config import Settings


@pytest.fixture
async def serve():
    runners = []

    async def start(application: web.Application) -> str:
        runner = web.AppRunner(application)
        await runner.setup()
        runners.append(runner)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port, *_ = runner.addresses[0]
        return f"http://{host}:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()


class TelegramRequest(BaseRequest):
    """Exercise actual PTB dispatch/serialization without contacting Telegram."""

    def __init__(self):
        self.calls = []
        self.message_ids = itertools.count(100)
        self.messages = {}
        self.failures = {}
        self.files = {}
        self.bot_name = "Mai"
        self.commands = []

    def photo(self, request_data, reference):
        upload = request_data.multipart_data.get(reference.removeprefix("attach://"))
        if upload is not None:
            content = upload[1]
            file_id = "photo-" + sha256(content).hexdigest()
            self.files[file_id] = content
        else:
            file_id = reference
        return {"file_id": file_id, "file_unique_id": file_id, "width": 500, "height": 500}

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        parameters = request_data.parameters if request_data else {}
        self.calls.append((name, parameters))
        if self.failures.get(name):
            failure = self.failures[name].pop(0)
            if isinstance(failure, dict):
                return failure["error_code"], json.dumps({"ok": False, **failure}).encode()
            return 400, json.dumps(
                {"ok": False, "error_code": 400, "description": failure}
            ).encode()
        if name == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "Mai", "username": "mai_test_bot"}
        elif name == "getMyName":
            result = {"name": self.bot_name}
        elif name == "setMyName":
            self.bot_name = parameters["name"]
            result = True
        elif name == "getMyCommands":
            result = self.commands
        elif name == "setMyCommands":
            self.commands = parameters["commands"]
            result = True
        elif name in {"sendMessage", "sendPhoto"}:
            result = {
                "message_id": next(self.message_ids),
                "date": 1700000000,
                "from": {"id": 123456, "is_bot": True, "first_name": "Mai"},
                "chat": {"id": int(parameters["chat_id"]), "type": "private"},
                "has_protected_content": parameters.get("protect_content", False),
            }
            if name == "sendPhoto":
                result["photo"] = [self.photo(request_data, parameters.get("photo", "photo"))]
                result["caption"] = parameters.get("caption", "")
            else:
                result["text"] = parameters["text"]
            if parameters.get("reply_markup"):
                result["reply_markup"] = parameters["reply_markup"]
            self.messages[(result["chat"]["id"], result["message_id"])] = result
        elif name in {
            "editMessageText",
            "editMessageCaption",
            "editMessageMedia",
            "editMessageReplyMarkup",
        }:
            key = int(parameters["chat_id"]), parameters["message_id"]
            previous = self.messages.get(key)
            if previous is None:
                return 400, json.dumps(
                    {"ok": False, "error_code": 400, "description": "Message to edit not found"}
                ).encode()
            result = previous.copy()
            if name == "editMessageText":
                if result.get("photo"):
                    return 400, json.dumps(
                        {
                            "ok": False,
                            "error_code": 400,
                            "description": "There is no text in the message to edit",
                        }
                    ).encode()
                result["text"] = parameters["text"]
            elif name == "editMessageCaption":
                result["caption"] = parameters.get("caption", "")
            elif name == "editMessageMedia":
                media = parameters["media"]
                if isinstance(media, str):
                    media = json.loads(media)
                result.pop("text", None)
                result["photo"] = [self.photo(request_data, media["media"])]
                result["caption"] = media.get("caption", "")
            if parameters.get("reply_markup"):
                result["reply_markup"] = parameters["reply_markup"]
            else:
                result.pop("reply_markup", None)
            if result == previous and name != "editMessageMedia":
                return 400, json.dumps(
                    {"ok": False, "error_code": 400, "description": "Message is not modified"}
                ).encode()
            self.messages[key] = result
        elif name == "deleteMessage":
            self.messages.pop((int(parameters["chat_id"]), parameters["message_id"]), None)
            result = True
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@dataclass
class BotHarness:
    app: object
    request: TelegramRequest
    counter: object

    @property
    def handlers(self):
        return self.app.bot_data["handlers"]

    async def send(self, user_id: int, text: str, *, chat_type="private"):
        message = {
            "message_id": next(self.counter),
            "date": 1700000000,
            "from": {"id": user_id, "is_bot": False, "first_name": "Player"},
            "chat": {"id": user_id if chat_type == "private" else -1000, "type": chat_type},
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        update = Update.de_json({"update_id": next(self.counter), "message": message}, self.app.bot)
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    def message(self, user_id: int, message_id: int | None = None):
        if message_id is not None:
            return self.request.messages[(user_id, message_id)]
        return [
            message for (chat_id, _), message in self.request.messages.items() if chat_id == user_id
        ][-1]

    async def click(
        self,
        user_id: int,
        data: str,
        *,
        message_id: int | None = None,
        inaccessible: bool = False,
    ):
        if message_id is None:
            matching = [
                message
                for (chat_id, _), message in self.request.messages.items()
                if chat_id == user_id
                and any(
                    button.get("callback_data") == data
                    for row in message.get("reply_markup", {}).get("inline_keyboard", [])
                    for button in row
                )
            ]
            message = matching[-1] if matching else self.message(user_id)
        else:
            message = self.message(user_id, message_id)
        if inaccessible:
            message = {"message_id": message["message_id"], "chat": message["chat"], "date": 0}
        update = Update.de_json(
            {
                "update_id": next(self.counter),
                "callback_query": {
                    "id": str(next(self.counter)),
                    "from": {"id": user_id, "is_bot": False, "first_name": "Player"},
                    "chat_instance": "test-chat",
                    "data": data,
                    "message": message,
                },
            },
            self.app.bot,
        )
        await self.app.update_processor.process_update(update, self.app.process_update(update))


@pytest.fixture
def bot_settings(tmp_path):
    return Settings(bot_token="123456:TEST_TOKEN", database_path=tmp_path / "bot.sqlite3")


@pytest.fixture
async def bot(bot_settings):
    request = TelegramRequest()
    app = create_application(
        bot_settings,
        bot=ExtBot(token="123456:TEST_TOKEN", request=request),
    )
    await app.initialize()
    await app.post_init(app)
    app.bot_data["handlers"].client.get_net_url = AsyncMock(
        side_effect=lambda user: f"https://maimai.wahlap.com/maimai-mobile/?t=user-{user.user_id}"
    )
    try:
        yield BotHarness(app, request, itertools.count(1))
    finally:
        await app.post_stop(app)
        await app.shutdown()
        await app.post_shutdown(app)
