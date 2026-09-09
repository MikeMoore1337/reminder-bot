from __future__ import annotations

from aiogram.types import Message

PRIVATE_CHAT_ONLY_TEXT = "Эта команда доступна только в личном чате с ботом."


def is_private_chat(message: Message) -> bool:
    return getattr(message.chat, "type", None) == "private"


async def require_private_chat(message: Message) -> bool:
    if is_private_chat(message):
        return True
    await message.answer(PRIVATE_CHAT_ONLY_TEXT)
    return False
