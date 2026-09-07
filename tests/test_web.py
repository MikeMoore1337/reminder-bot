import asyncio
import json
from types import SimpleNamespace

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
