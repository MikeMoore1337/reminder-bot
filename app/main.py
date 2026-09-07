from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from app.bot_commands import setup_bot_commands
from app.bot_factory import create_bot, create_dispatcher
from app.config import get_settings
from app.logging_config import setup_logging
from app.web import build_web_app

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


async def run_polling() -> None:
    logger.info("Starting bot application in polling mode")
    bot = create_bot()
    dp = create_dispatcher()

    await setup_bot_commands(bot)

    try:
        await dp.start_polling(bot, allowed_updates=settings.allowed_updates)
    finally:
        await bot.session.close()
        logger.info("Bot application stopped")


async def run_webhook(stop_event: asyncio.Event | None = None) -> None:
    logger.info(
        "Starting bot application in webhook mode",
        extra={
            "extra_data": f"url={settings.webhook_url} host={settings.app_host} port={settings.app_port}"
        },
    )
    if not settings.webhook_url:
        raise RuntimeError("WEBHOOK_BASE_URL must be set in webhook mode")
    if not settings.webhook_secret_token:
        raise RuntimeError("WEBHOOK_SECRET_TOKEN must be set in webhook mode")

    bot = create_bot()
    dp = create_dispatcher()

    await setup_bot_commands(bot)

    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret_token,
        allowed_updates=settings.allowed_updates,
        drop_pending_updates=False,
    )

    app = build_web_app(bot, dp)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.app_host, port=settings.app_port)

    try:
        await site.start()
        logger.info("Webhook server started")
        if stop_event is None:
            while True:
                await asyncio.sleep(3600)
        else:
            await stop_event.wait()
    finally:
        await bot.delete_webhook(drop_pending_updates=False)
        await runner.cleanup()
        await bot.session.close()
        logger.info("Webhook application stopped")


async def main() -> None:
    if settings.normalized_bot_mode == "webhook":
        await run_webhook()
        return
    await run_polling()


if __name__ == "__main__":
    asyncio.run(main())
