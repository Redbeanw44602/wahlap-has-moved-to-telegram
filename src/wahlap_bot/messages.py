"""Update existing bot messages when navigating inline keyboards."""

from contextlib import suppress
from io import BytesIO

from telegram import InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Message, Update
from telegram.error import BadRequest, TelegramError


async def _delete_message(message: Message) -> None:
    try:
        await message.delete()
    except BadRequest as exc:
        if "message to delete not found" not in exc.message.casefold():
            raise


def same_message(first: Message | None, second: Message | None) -> bool:
    return bool(
        first is not None
        and second is not None
        and first.chat_id == second.chat_id
        and first.message_id == second.message_id
    )


def _cannot_edit(error: BadRequest) -> bool:
    return any(
        reason in error.message.casefold()
        for reason in (
            "message to edit not found",
            "message can't be edited",
            "message cannot be edited",
            "there is no text in the message to edit",
        )
    )


async def clear_buttons(message: Message | None) -> None:
    if message is not None and message.is_accessible and message.reply_markup is not None:
        with suppress(TelegramError):
            await message.edit_reply_markup(reply_markup=None)


async def show_message(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    target: Message | None = None,
) -> Message:
    if target is None and update.callback_query is not None:
        target = update.callback_query.message
    if isinstance(target, Message) and target.is_accessible:
        if target.photo:
            # A text screen or refresh must remove the actual QR message first.
            await _delete_message(target)
        else:
            try:
                return await target.edit_text(
                    text,
                    reply_markup=reply_markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except BadRequest as exc:
                if "message is not modified" in exc.message.casefold():
                    return target
                if not _cannot_edit(exc):
                    raise
            await clear_buttons(target)
    send = (
        update.effective_message.reply_text
        if update.effective_message is not None
        else update.effective_chat.send_message
    )
    return await send(
        text,
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        # Edits inherit protection, including a later conversion to a QR image.
        protect_content=True,
    )


async def show_photo(
    target: Message, image: BytesIO, caption: str, reply_markup: InlineKeyboardMarkup
) -> Message:
    try:
        # editMessageMedia can also add a photo to a text message.
        return await target.edit_media(
            InputMediaPhoto(image, caption=caption), reply_markup=reply_markup
        )
    except BadRequest as exc:
        if "message is not modified" in exc.message.casefold():
            return target
        if not _cannot_edit(exc):
            raise
    await _delete_message(target)
    image.seek(0)
    return await target.reply_photo(
        image, caption=caption, reply_markup=reply_markup, protect_content=True
    )
