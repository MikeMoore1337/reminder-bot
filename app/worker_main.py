from __future__ import annotations

import asyncio
import logging
import signal

from app.bot_factory import create_bot
from app.config import get_settings
from app.logging_config import setup_logging
from app.workers.reminder_worker import reminder_loop

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


def _install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.info(
                "Signal handler is unavailable; worker cancellation remains lease-recoverable",
                extra={"extra_data": f"signal={signum.name}"},
            )


async def main() -> None:
    logger.info("Starting reminder worker application")
    bot = create_bot()
    stop_event = asyncio.Event()
    _install_shutdown_handlers(stop_event)
    try:
        await reminder_loop(bot, stop_event=stop_event)
    finally:
        await bot.session.close()
        logger.info("Reminder worker application stopped")


if __name__ == "__main__":
    asyncio.run(main())
