from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            logger.info(
                "Incoming message",
                extra={
                    "extra_data": (
                        f"user_id={event.from_user.id if event.from_user else 'unknown'} "
                        f"chat_id={event.chat.id if event.chat else 'unknown'} "
                        f"text_length={len(event.text or '')}"
                    )
                },
            )

        elif isinstance(event, CallbackQuery):
            logger.info(
                "Incoming callback",
                extra={
                    "extra_data": (
                        f"user_id={event.from_user.id if event.from_user else 'unknown'} "
                        f"chat_id={event.message.chat.id if event.message else 'unknown'} "
                        f"data_length={len(event.data or '')}"
                    )
                },
            )

        return await handler(event, data)
