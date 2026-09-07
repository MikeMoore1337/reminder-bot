from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def healthcheck(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def readinesscheck(_: web.Request) -> web.Response:
    """Report whether the process can reach PostgreSQL within a bounded timeout."""

    try:
        async with asyncio.timeout(get_settings().readiness_timeout_seconds):
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning(
            "Database readiness check failed",
            extra={"extra_data": f"error_type={type(exc).__name__[:80]}"},
        )
        return web.json_response({"status": "not_ready"}, status=503)

    return web.json_response({"status": "ok"})


def build_web_app(bot: Bot, dispatcher: Dispatcher) -> web.Application:
    settings = get_settings()
    app = web.Application()
    app.router.add_get("/healthz", healthcheck)
    app.router.add_get("/readyz", readinesscheck)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret_token,
    )
    webhook_handler.register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    logger.info("Webhook app configured", extra={"extra_data": f"path={settings.webhook_path}"})
    return app
