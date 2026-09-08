import asyncio
import socket
from collections.abc import Mapping

import pytest
from aiohttp.abc import AbstractResolver

from app.services.condition_provider import (
    ConditionObservation,
    ConditionProviderError,
    HttpConditionProvider,
    normalize_provider_config,
    validate_https_target,
)


class _PublicResolver(AbstractResolver):
    def __init__(self, addresses: list[str] | None = None, *, delay: float = 0) -> None:
        self.addresses = addresses or ["93.184.216.34"]
        self.delay = delay

    async def resolve(
        self,
        host: str,
        port: int,
        family: int = socket.AF_INET,
    ) -> list[dict[str, int | str]]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return [
            {
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self) -> None:
        return None


class _Content:
    def __init__(self, body: bytes, *, chunk_size: int = 32_768) -> None:
        self.body = body
        self.chunk_size = chunk_size
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.body):
            return b""
        requested = len(self.body) - self.offset if size < 0 else min(size, self.chunk_size)
        chunk = self.body[self.offset : self.offset + requested]
        self.offset += len(chunk)
        return chunk


class _Response:
    def __init__(self, status: int, body: bytes = b'{"state":"open"}', **headers: str) -> None:
        self.status = status
        self.headers: Mapping[str, str] = headers
        self.content = _Content(body)


class _RequestContext:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self) -> _Response:
        return self.response

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Session:
    def __init__(self, response: _Response, captured: dict[str, object]) -> None:
        self.response = response
        self.captured = captured

    def get(self, target: str, **kwargs: object) -> _RequestContext:
        self.captured["target"] = target
        self.captured.update(kwargs)
        return _RequestContext(self.response)

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _SessionFactory:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.captured: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> _Session:
        self.captured["session_kwargs"] = kwargs
        return _Session(self.response, self.captured)


def _provider(response: _Response, *, resolver: AbstractResolver | None = None, **kwargs):
    factory = _SessionFactory(response)
    kwargs.setdefault("authorization_env_allowlist", {"CONDITION_TEST_TOKEN"})
    provider = HttpConditionProvider(
        resolver=resolver or _PublicResolver(),
        session_factory=factory,
        **kwargs,
    )
    return provider, factory


@pytest.mark.asyncio
async def test_http_json_provider_normalizes_state_and_uses_secret_reference(monkeypatch) -> None:
    monkeypatch.setenv("CONDITION_TEST_TOKEN", "secret-value")
    provider, factory = _provider(_Response(200, b'{"state":"In Progress"}'))

    observation = await provider.observe(
        "https://example.com/status",
        config={"authorization_env_var": "condition_test_token"},
    )

    assert observation == ConditionObservation(
        state="in_progress", fingerprint=observation.fingerprint
    )
    assert observation.fingerprint is not None
    assert len(observation.fingerprint) == 64
    assert factory.captured["target"] == "https://example.com/status"
    assert factory.captured["allow_redirects"] is False
    assert factory.captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer secret-value",
    }


@pytest.mark.parametrize(
    "target,code",
    [
        ("http://example.com/status", "invalid_target"),
        ("https://user:pass@example.com/status", "invalid_target"),
        ("https://example.com/status#fragment", "invalid_target"),
        ("https://example.com:8443/status", "invalid_target"),
        ("https://127.0.0.1/status", "private_target"),
        ("https://example.com/status?access_token=secret", "query_not_allowed"),
        ("https://example.com/status?client_secret=secret", "query_not_allowed"),
        ("https://example.com/status?auth_token=secret", "query_not_allowed"),
        ("https://example.com/status?signature=secret", "query_not_allowed"),
        ("https://example.com/status?", "query_not_allowed"),
    ],
)
def test_https_target_validation_fails_closed(target: str, code: str) -> None:
    with pytest.raises(ConditionProviderError) as error:
        validate_https_target(target)
    assert error.value.code == code


def test_provider_config_persists_only_environment_variable_reference() -> None:
    assert normalize_provider_config(
        {"authorization_env_var": "provider_token"},
        authorization_env_allowlist={"provider_token"},
    ) == {"authorization_env_var": "PROVIDER_TOKEN"}
    with pytest.raises(ConditionProviderError, match="authorization_not_allowed"):
        normalize_provider_config(
            {"authorization_env_var": "provider_token"},
            authorization_env_allowlist={"other_provider_token"},
        )
    with pytest.raises(ConditionProviderError, match="authorization_not_allowed"):
        normalize_provider_config(
            {"authorization_env_var": "BOT_TOKEN"},
            authorization_env_allowlist={"provider_token"},
        )
    with pytest.raises(ConditionProviderError, match="invalid_authorization_allowlist"):
        normalize_provider_config(
            {"authorization_env_var": "BOT_TOKEN"},
            authorization_env_allowlist={"BOT_TOKEN"},
        )
    with pytest.raises(ConditionProviderError, match="invalid_provider_config"):
        normalize_provider_config({"Authorization": "Bearer secret-value"})


@pytest.mark.asyncio
async def test_private_dns_is_rejected_before_request() -> None:
    provider, factory = _provider(
        _Response(200),
        resolver=_PublicResolver(["10.0.0.8"]),
    )

    with pytest.raises(ConditionProviderError, match="private_target"):
        await provider.observe("https://example.com/status")
    assert "target" not in factory.captured


@pytest.mark.asyncio
async def test_redirect_is_rejected_and_never_followed() -> None:
    provider, factory = _provider(_Response(302, Location="https://other.example/next"))

    with pytest.raises(ConditionProviderError) as error:
        await provider.observe("https://example.com/status")

    assert error.value.code == "redirect_not_allowed"
    assert factory.captured["allow_redirects"] is False


@pytest.mark.asyncio
async def test_content_length_and_chunked_body_are_bounded() -> None:
    provider, _ = _provider(
        _Response(200, b"x" * 20, **{"Content-Length": "20"}), max_response_bytes=10
    )
    with pytest.raises(ConditionProviderError, match="response_too_large"):
        await provider.observe("https://example.com/status")

    provider, _ = _provider(
        _Response(200, b'{"state":"' + b"x" * 40 + b'"}'), max_response_bytes=20
    )
    with pytest.raises(ConditionProviderError, match="response_too_large"):
        await provider.observe("https://example.com/status")


@pytest.mark.asyncio
async def test_timeout_and_rate_limit_are_safe_and_bounded(monkeypatch) -> None:
    provider, _ = _provider(
        _Response(200),
        resolver=_PublicResolver(delay=0.05),
        timeout_seconds=0.01,
    )
    with pytest.raises(ConditionProviderError, match="timeout"):
        await provider.observe("https://example.com/status")

    provider, _ = _provider(_Response(429, **{"Retry-After": "999999"}))
    with pytest.raises(ConditionProviderError) as error:
        await provider.observe("https://example.com/status")
    assert error.value.code == "rate_limited"
    assert error.value.retry_after_seconds == 86_400

    monkeypatch.setenv("CONDITION_TEST_TOKEN", "very-secret")
    with pytest.raises(ConditionProviderError) as error:
        await provider.observe(
            "https://example.com/status",
            config={"authorization_env_var": "MISSING_CONDITION_TOKEN"},
        )
    assert "very-secret" not in str(error.value)
