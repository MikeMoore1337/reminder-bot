import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import app.main as main_module


class _BotSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Bot:
    def __init__(self) -> None:
        self.session = _BotSession()
        self.set_webhook_calls: list[dict[str, object]] = []
        self.delete_webhook_calls: list[dict[str, object]] = []

    async def set_webhook(self, **kwargs) -> None:
        self.set_webhook_calls.append(kwargs)

    async def delete_webhook(self, **kwargs) -> None:
        self.delete_webhook_calls.append(kwargs)


class _Runner:
    def __init__(self) -> None:
        self.setup_calls = 0
        self.cleanup_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


class _Site:
    def __init__(self) -> None:
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1


def test_webhook_shutdown_runs_cleanup_and_closes_bot(monkeypatch) -> None:
    async def scenario() -> None:
        bot = _Bot()
        runner = _Runner()
        site = _Site()
        stop_event = asyncio.Event()
        stop_event.set()

        monkeypatch.setattr(
            main_module,
            "settings",
            SimpleNamespace(
                webhook_url="https://example.test/telegram/webhook",
                webhook_secret_token="test-secret",
                allowed_updates=["message"],
                app_host="127.0.0.1",
                app_port=8080,
            ),
        )
        monkeypatch.setattr(main_module, "create_bot", lambda: bot)
        monkeypatch.setattr(main_module, "create_dispatcher", lambda: object())

        async def setup_commands(_bot) -> None:
            return None

        monkeypatch.setattr(main_module, "setup_bot_commands", setup_commands)
        monkeypatch.setattr(main_module, "build_web_app", lambda _bot, _dp: object())
        monkeypatch.setattr(main_module.web, "AppRunner", lambda _app: runner)
        monkeypatch.setattr(
            main_module.web,
            "TCPSite",
            lambda _runner, host, port: site,
        )

        await main_module.run_webhook(stop_event=stop_event)

        assert site.start_calls == 1
        assert bot.set_webhook_calls[0]["url"] == "https://example.test/telegram/webhook"
        assert bot.delete_webhook_calls == [{"drop_pending_updates": False}]
        assert runner.setup_calls == 1
        assert runner.cleanup_calls == 1
        assert bot.session.closed is True

    asyncio.run(scenario())
