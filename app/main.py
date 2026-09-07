from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from app.bot_commands import setup_bot_commands
from app.bot_factory import create_bot, create_dispatcher
from app.config import get_settings
from app.logging_config import setup_logging
from app.web import build_probe_app, build_web_app

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


def _install_shutdown_handlers(
    stop_event: asyncio.Event,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    if loop is None:
        loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.info(
                "Signal handler is unavailable; webhook shutdown remains runtime-managed",
                extra={"extra_data": f"signal={signum.name}"},
            )


async def run_polling() -> None:
    logger.info("Starting bot application in polling mode")
    bot = create_bot()
    dp = create_dispatcher()

    runner: web.AppRunner | None = None

    try:
        await setup_bot_commands(bot)

        runner = web.AppRunner(build_probe_app())
        await runner.setup()
        site = web.TCPSite(runner, host=settings.app_host, port=settings.app_port)
        await site.start()
        logger.info(
            "Probe server started",
            extra={"extra_data": f"host={settings.app_host} port={settings.app_port}"},
        )
        await dp.start_polling(bot, allowed_updates=settings.allowed_updates)
    finally:
        if runner is not None:
            await runner.cleanup()
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
        stop_event = asyncio.Event()
        _install_shutdown_handlers(stop_event)
        await run_webhook(stop_event)
        return
    await run_polling()


if __name__ == "__main__":
    asyncio.run(main())
