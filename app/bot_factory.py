from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.handlers.admin import router as admin_router
from app.handlers.reminders import router as reminders_router
from app.handlers.shared import router as shared_router
from app.handlers.ui import router as ui_router
from app.middlewares.logging import LoggingMiddleware

settings = get_settings()


def create_bot() -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    dp.message.middleware(LoggingMiddleware())
    dp.callback_query.middleware(LoggingMiddleware())

    dp.include_router(ui_router)
    dp.include_router(shared_router)
    dp.include_router(reminders_router)
    dp.include_router(admin_router)

    return dp
