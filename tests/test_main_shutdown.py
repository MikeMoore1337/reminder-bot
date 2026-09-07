import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

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


class _SignalLoop:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.handlers: dict[signal.Signals, object] = {}

    def add_signal_handler(self, signum, callback) -> None:
        if self.failure is not None:
            raise self.failure
        self.handlers[signum] = callback


def test_webhook_main_creates_and_passes_stop_event(monkeypatch) -> None:
    async def scenario() -> None:
        installed_events: list[asyncio.Event] = []
        passed_events: list[asyncio.Event] = []

        monkeypatch.setattr(
            main_module,
            "settings",
            SimpleNamespace(normalized_bot_mode="webhook"),
        )

        def install_handlers(stop_event: asyncio.Event) -> None:
            installed_events.append(stop_event)

        async def fake_run_webhook(stop_event: asyncio.Event) -> None:
            passed_events.append(stop_event)

        monkeypatch.setattr(main_module, "_install_shutdown_handlers", install_handlers)
        monkeypatch.setattr(main_module, "run_webhook", fake_run_webhook)

        await main_module.main()

        assert len(installed_events) == 1
        assert passed_events == installed_events
        assert installed_events[0].is_set() is False

    asyncio.run(scenario())


def test_webhook_signal_handlers_set_event_for_sigint_and_sigterm() -> None:
    async def scenario() -> None:
        stop_event = asyncio.Event()
        loop = _SignalLoop()

        main_module._install_shutdown_handlers(stop_event, loop=loop)

        assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}
        loop.handlers[signal.SIGTERM]()
        assert stop_event.is_set() is True

        stop_event.clear()
        loop.handlers[signal.SIGINT]()
        assert stop_event.is_set() is True

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [NotImplementedError(), RuntimeError("not main thread")])
def test_unsupported_signal_handler_platform_does_not_crash(failure: Exception) -> None:
    async def scenario() -> None:
        main_module._install_shutdown_handlers(asyncio.Event(), loop=_SignalLoop(failure))

    asyncio.run(scenario())


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


def test_sigterm_handler_drives_webhook_cleanup(monkeypatch) -> None:
    async def scenario() -> None:
        bot = _Bot()
        runner = _Runner()
        site = _Site()
        stop_event = asyncio.Event()
        signal_loop = _SignalLoop()

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

        main_module._install_shutdown_handlers(stop_event, loop=signal_loop)
        task = asyncio.create_task(main_module.run_webhook(stop_event=stop_event))
        await asyncio.sleep(0)

        signal_loop.handlers[signal.SIGTERM]()
        await task

        assert stop_event.is_set() is True
        assert bot.delete_webhook_calls == [{"drop_pending_updates": False}]
        assert runner.cleanup_calls == 1
        assert bot.session.closed is True

    asyncio.run(scenario())
