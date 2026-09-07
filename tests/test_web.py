import asyncio
import json
from types import SimpleNamespace

from aiohttp import web

import app.web as web_module


class _Session:
    def __init__(self, failure: Exception | None = None, delay: float = 0.0) -> None:
        self.failure = failure
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def execute(self, statement) -> None:
        del statement
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure


def _response_body(response) -> dict[str, str]:
    return json.loads(response.text)


def _route_paths(app) -> set[str]:
    return {route.resource.canonical for route in app.router.routes()}


def test_probe_app_contains_only_liveness_and_readiness_routes() -> None:
    app = web_module.build_probe_app()

    assert _route_paths(app) == {"/healthz", "/readyz"}


def test_webhook_app_adds_telegram_route_to_probe_routes(monkeypatch) -> None:
    class _WebhookHandler:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def register(self, app, path: str) -> None:
            async def handle(_request):
                return web.Response()

            app.router.add_post(path, handle)

    monkeypatch.setattr(
        web_module,
        "get_settings",
        lambda: SimpleNamespace(
            webhook_secret_token="test-secret",
            webhook_path="/telegram/webhook",
        ),
    )
    monkeypatch.setattr(web_module, "SimpleRequestHandler", _WebhookHandler)
    monkeypatch.setattr(web_module, "setup_application", lambda *args, **kwargs: None)

    app = web_module.build_web_app(object(), object())

    assert _route_paths(app) == {"/healthz", "/readyz", "/telegram/webhook"}


def test_healthz_is_liveness_only(monkeypatch) -> None:
    def fail_if_database_is_touched():
        raise AssertionError("liveness must not open a database session")

    monkeypatch.setattr(web_module, "SessionLocal", fail_if_database_is_touched)

    response = asyncio.run(web_module.healthcheck(None))

    assert response.status == 200
    assert _response_body(response) == {"status": "ok"}


def test_readyz_returns_200_for_a_healthy_database(monkeypatch) -> None:
    monkeypatch.setattr(
        web_module,
        "get_settings",
        lambda: SimpleNamespace(readiness_timeout_seconds=1.0),
    )
    monkeypatch.setattr(web_module, "SessionLocal", lambda: _Session())

    response = asyncio.run(web_module.readinesscheck(None))

    assert response.status == 200
    assert _response_body(response) == {"status": "ok"}


def test_readyz_returns_503_without_exposing_database_error(monkeypatch) -> None:
    monkeypatch.setattr(
        web_module,
        "get_settings",
        lambda: SimpleNamespace(readiness_timeout_seconds=1.0),
    )
    monkeypatch.setattr(
        web_module,
        "SessionLocal",
        lambda: _Session(RuntimeError("password=super-secret host=db")),
    )

    response = asyncio.run(web_module.readinesscheck(None))

    assert response.status == 503
    assert _response_body(response) == {"status": "not_ready"}
    assert "super-secret" not in response.text
    assert "password" not in response.text


def test_readyz_timeout_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        web_module,
        "get_settings",
        lambda: SimpleNamespace(readiness_timeout_seconds=0.01),
    )
    monkeypatch.setattr(web_module, "SessionLocal", lambda: _Session(delay=1.0))

    response = asyncio.run(web_module.readinesscheck(None))

    assert response.status == 503
    assert _response_body(response) == {"status": "not_ready"}
