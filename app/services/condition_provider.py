from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

MAX_CONDITION_STATE_LENGTH = 64
MAX_CONDITION_FINGERPRINT_LENGTH = 128
MAX_CONDITION_TARGET_LENGTH = 2048
MAX_AUTHORIZATION_ENV_NAME_LENGTH = 128
MAX_AUTHORIZATION_VALUE_LENGTH = 4096
MAX_RETRY_AFTER_SECONDS = 86_400
TELEGRAM_MESSAGE_LIMIT = 4096

_PROVIDER_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_STATE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
RESERVED_RUNTIME_ENV_NAMES = frozenset(
    {
        "ADMIN_IDS",
        "APP_HOST",
        "APP_PORT",
        "BOT_MODE",
        "BOT_TOKEN",
        "CONDITION_AUTHORIZATION_ENV_ALLOWLIST",
        "CONDITION_AUTHORIZATION_ENV_ALLOWLIST_RAW",
        "DATABASE_URL",
        "DEPLOY_ENABLED",
        "DEFAULT_TIMEZONE",
        "LOG_LEVEL",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POLLING_ALLOWED_UPDATES",
        "WEBHOOK_BASE_URL",
        "WEBHOOK_PATH",
        "WEBHOOK_SECRET_TOKEN",
    }
)


class ConditionProviderError(RuntimeError):
    """Safe, bounded provider failure classification.

    The exception intentionally contains only a stable code.  The target URL,
    response body, authorization value, and upstream exception are never part
    of its public string representation.
    """

    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        normalized_code = code.strip().lower()
        if not _ERROR_CODE_PATTERN.fullmatch(normalized_code):
            normalized_code = "provider_error"
        if retry_after_seconds is not None:
            retry_after_seconds = max(1, min(int(retry_after_seconds), MAX_RETRY_AFTER_SECONDS))
        if status_code is not None and not 100 <= int(status_code) <= 599:
            status_code = None
        self.code = normalized_code
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        super().__init__(f"condition_provider_error:{normalized_code}")


def normalize_provider_type(value: str) -> str:
    if not isinstance(value, str):
        raise ConditionProviderError("invalid_provider_type")
    normalized = value.strip().lower()
    if not _PROVIDER_TYPE_PATTERN.fullmatch(normalized):
        raise ConditionProviderError("invalid_provider_type")
    return normalized


def normalize_condition_state(value: str) -> str:
    if not isinstance(value, str):
        raise ConditionProviderError("malformed_state")
    normalized = re.sub(r"\s+", "_", value.strip().casefold())
    if not normalized or len(normalized) > MAX_CONDITION_STATE_LENGTH:
        raise ConditionProviderError("malformed_state")
    if not _STATE_PATTERN.fullmatch(normalized):
        raise ConditionProviderError("malformed_state")
    return normalized


def normalize_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConditionProviderError("malformed_fingerprint")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_CONDITION_FINGERPRINT_LENGTH:
        raise ConditionProviderError("malformed_fingerprint")
    if any(ord(char) < 32 for char in normalized):
        raise ConditionProviderError("malformed_fingerprint")
    return normalized


@dataclass(frozen=True, slots=True)
class ConditionObservation:
    """Normalized provider result; raw provider payloads do not cross this boundary."""

    state: str
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", normalize_condition_state(self.state))
        object.__setattr__(self, "fingerprint", normalize_fingerprint(self.fingerprint))


class ConditionProvider(Protocol):
    provider_type: str

    async def observe(
        self,
        target: str,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> ConditionObservation: ...


def _normalize_authorization_allowlist(
    authorization_env_allowlist: Collection[str] | None,
) -> frozenset[str]:
    normalized: set[str] = set()
    for value in authorization_env_allowlist or ():
        if not isinstance(value, str):
            raise ConditionProviderError("invalid_authorization_allowlist")
        name = value.strip().upper()
        if not _ENV_NAME_PATTERN.fullmatch(name) or name in RESERVED_RUNTIME_ENV_NAMES:
            raise ConditionProviderError("invalid_authorization_allowlist")
        normalized.add(name)
    return frozenset(normalized)


def normalize_provider_config(
    config: Mapping[str, Any] | None,
    *,
    authorization_env_allowlist: Collection[str] | None = None,
) -> dict[str, str]:
    """Keep persisted provider configuration to non-secret references only."""

    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise ConditionProviderError("invalid_provider_config")
    unknown_keys = set(config) - {"authorization_env_var"}
    if unknown_keys:
        raise ConditionProviderError("invalid_provider_config")

    env_name = config.get("authorization_env_var")
    if env_name is None:
        return {}
    if not isinstance(env_name, str):
        raise ConditionProviderError("invalid_authorization_reference")
    normalized_name = env_name.strip().upper()
    if len(normalized_name) > MAX_AUTHORIZATION_ENV_NAME_LENGTH or not _ENV_NAME_PATTERN.fullmatch(
        normalized_name
    ):
        raise ConditionProviderError("invalid_authorization_reference")
    allowed_names = _normalize_authorization_allowlist(authorization_env_allowlist)
    if normalized_name in RESERVED_RUNTIME_ENV_NAMES or normalized_name not in allowed_names:
        raise ConditionProviderError("authorization_not_allowed")
    return {"authorization_env_var": normalized_name}


def serialize_provider_config(
    config: Mapping[str, Any] | None,
    *,
    authorization_env_allowlist: Collection[str] | None = None,
) -> str:
    return json.dumps(
        normalize_provider_config(
            config,
            authorization_env_allowlist=authorization_env_allowlist,
        ),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_provider_config(
    serialized: str | None,
    *,
    authorization_env_allowlist: Collection[str] | None = None,
) -> dict[str, str]:
    if not serialized:
        return {}
    try:
        value = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ConditionProviderError("invalid_provider_config") from exc
    return normalize_provider_config(
        value,
        authorization_env_allowlist=authorization_env_allowlist,
    )


@dataclass(frozen=True, slots=True)
class ValidatedConditionTarget:
    url: str
    hostname: str
    port: int


def validate_https_target(target: str) -> ValidatedConditionTarget:
    if not isinstance(target, str) or not target or len(target) > MAX_CONDITION_TARGET_LENGTH:
        raise ConditionProviderError("invalid_target")
    if any(char.isspace() or ord(char) < 32 for char in target):
        raise ConditionProviderError("invalid_target")
    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConditionProviderError("invalid_target") from exc

    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConditionProviderError("invalid_target")
    if port is not None and port != 443:
        raise ConditionProviderError("invalid_target")
    if hostname.casefold() == "localhost" or hostname.casefold().endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ConditionProviderError("private_target")

    try:
        hostname_ascii = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ConditionProviderError("invalid_target") from exc

    try:
        literal_address = ipaddress.ip_address(hostname_ascii)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise ConditionProviderError("private_target")

    if parsed.query or "?" in target:
        raise ConditionProviderError("query_not_allowed")

    return ValidatedConditionTarget(
        url=target,
        hostname=hostname_ascii,
        port=port or 443,
    )


def _address_from_resolution(value: Mapping[str, Any]) -> ResolveResult:
    try:
        host = str(value["host"])
        port = int(value.get("port", 443))
        family = int(value.get("family", socket.AF_UNSPEC))
        proto = int(value.get("proto", 0))
        flags = int(value.get("flags", 0))
        address = ipaddress.ip_address(host)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConditionProviderError("dns_invalid_result") from exc
    if not address.is_global:
        raise ConditionProviderError("private_target")
    return ResolveResult(
        hostname=host,
        host=str(address),
        port=port,
        family=family,
        proto=proto,
        flags=flags,
    )


async def resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    resolver: AbstractResolver,
) -> list[ResolveResult]:
    try:
        resolved = await resolver.resolve(hostname, port, family=socket.AF_UNSPEC)
    except (OSError, socket.gaierror, aiohttp.ClientError) as exc:
        raise ConditionProviderError("dns_resolution_failed") from exc
    if not resolved:
        raise ConditionProviderError("dns_resolution_failed")

    addresses: list[ResolveResult] = []
    for item in resolved:
        addresses.append(_address_from_resolution(item))
    return addresses


class _PinnedResolver(AbstractResolver):
    """Replay the already validated DNS answer to prevent a second DNS lookup."""

    def __init__(self, addresses: list[ResolveResult]) -> None:
        self._addresses = addresses

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        result = [
            ResolveResult(
                hostname=address["hostname"],
                host=address["host"],
                port=port,
                family=address["family"],
                proto=address["proto"],
                flags=address["flags"],
            )
            for address in self._addresses
        ]
        if family == socket.AF_UNSPEC:
            return result
        matching = [address for address in result if address.get("family") == family]
        return matching or result

    async def close(self) -> None:
        return None


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value.strip())
    except (AttributeError, ValueError):
        return None
    if parsed < 1:
        return None
    return min(parsed, MAX_RETRY_AFTER_SECONDS)


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return str(value)
    folded_name = name.casefold()
    for key, candidate in headers.items():
        if str(key).casefold() == folded_name:
            return str(candidate)
    return None


async def _read_bounded_response(response: Any, max_bytes: int) -> bytes:
    content = getattr(response, "content", None)
    reader = getattr(content, "read", None)
    if callable(reader):
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await reader(min(16_384, max_bytes + 1))
            if not chunk:
                return b"".join(chunks)
            if not isinstance(chunk, bytes):
                raise ConditionProviderError("malformed_response")
            total += len(chunk)
            if total > max_bytes:
                raise ConditionProviderError("response_too_large")
            chunks.append(chunk)

    read = getattr(response, "read", None)
    if not callable(read):
        raise ConditionProviderError("malformed_response")
    body = await read()
    if not isinstance(body, bytes) or len(body) > max_bytes:
        raise ConditionProviderError("response_too_large")
    return body


class HttpConditionProvider:
    """SSRF-safe reference provider for a bounded HTTPS JSON endpoint."""

    provider_type = "http_json"

    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        max_response_bytes: int = 65_536,
        resolver: AbstractResolver | None = None,
        resolver_factory: Callable[[], AbstractResolver] | None = None,
        session_factory: Callable[..., Any] | None = None,
        authorization_env_allowlist: Collection[str] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("HTTP condition provider bounds must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.resolver = resolver
        self.resolver_factory = resolver_factory
        self.session_factory = session_factory or aiohttp.ClientSession
        self.authorization_env_allowlist = _normalize_authorization_allowlist(
            authorization_env_allowlist
        )

    @staticmethod
    def validate_target(target: str) -> ValidatedConditionTarget:
        return validate_https_target(target)

    async def _request_once(
        self,
        target: ValidatedConditionTarget,
        headers: Mapping[str, str],
    ) -> ConditionObservation:
        resolver = self.resolver
        owns_resolver = resolver is None
        if resolver is None:
            resolver = (
                self.resolver_factory()
                if self.resolver_factory
                else aiohttp.resolver.ThreadedResolver()
            )

        try:
            try:
                ip_literal = ipaddress.ip_address(target.hostname)
            except ValueError:
                ip_literal = None
            if ip_literal is not None:
                if not ip_literal.is_global:
                    raise ConditionProviderError("private_target")
                addresses = [
                    ResolveResult(
                        hostname=target.hostname,
                        host=str(ip_literal),
                        port=target.port,
                        family=socket.AF_INET6 if ip_literal.version == 6 else socket.AF_INET,
                        proto=0,
                        flags=0,
                    )
                ]
            else:
                addresses = await resolve_public_addresses(
                    target.hostname,
                    target.port,
                    resolver=resolver,
                )

            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(addresses),
                ssl=True,
                use_dns_cache=False,
                limit=1,
            )
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                session_context = self.session_factory(
                    connector=connector,
                    timeout=timeout,
                    trust_env=False,
                )
                if hasattr(session_context, "__aenter__"):
                    async with session_context as session:
                        return await self._request_with_session(session, target.url, headers)
                session = session_context
                try:
                    return await self._request_with_session(session, target.url, headers)
                finally:
                    close = getattr(session, "close", None)
                    if callable(close):
                        result = close()
                        if inspect.isawaitable(result):
                            await result
            finally:
                await connector.close()
        finally:
            if owns_resolver:
                close = getattr(resolver, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    async def _request_with_session(
        self,
        session: Any,
        target: str,
        headers: Mapping[str, str],
    ) -> ConditionObservation:
        request_context = session.get(target, headers=dict(headers), allow_redirects=False)
        if hasattr(request_context, "__aenter__"):
            async with request_context as response:
                return await self._parse_response(response)
        response = (
            await request_context if inspect.isawaitable(request_context) else request_context
        )
        return await self._parse_response(response)

    async def _parse_response(self, response: Any) -> ConditionObservation:
        status = int(getattr(response, "status", 0))
        headers = getattr(response, "headers", {}) or {}
        if 300 <= status < 400:
            raise ConditionProviderError("redirect_not_allowed", status_code=status)
        if status == 429:
            raise ConditionProviderError(
                "rate_limited",
                retry_after_seconds=_retry_after(_header(headers, "Retry-After")),
                status_code=status,
            )
        if status != 200:
            raise ConditionProviderError("http_error", status_code=status)

        content_length = _header(headers, "Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise ConditionProviderError("invalid_content_length", status_code=status) from exc
            if parsed_length < 0 or parsed_length > self.max_response_bytes:
                raise ConditionProviderError("response_too_large", status_code=status)

        content_type = (_header(headers, "Content-Type") or "").split(";", 1)[0].strip().casefold()
        if (
            content_type
            and content_type != "application/json"
            and not content_type.endswith("+json")
        ):
            raise ConditionProviderError("invalid_content_type", status_code=status)

        body = await _read_bounded_response(response, self.max_response_bytes)
        digest = hashlib.sha256(body).hexdigest()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError) as exc:
            raise ConditionProviderError("malformed_response", status_code=status) from exc
        if not isinstance(payload, dict):
            raise ConditionProviderError("malformed_response", status_code=status)
        state = payload.get("state")
        if not isinstance(state, str):
            raise ConditionProviderError("malformed_response", status_code=status)
        return ConditionObservation(state=state, fingerprint=digest)

    async def observe(
        self,
        target: str,
        *,
        config: Mapping[str, Any] | None = None,
    ) -> ConditionObservation:
        validated_target = validate_https_target(target)
        normalized_config = normalize_provider_config(
            config,
            authorization_env_allowlist=self.authorization_env_allowlist,
        )
        headers = {"Accept": "application/json"}
        env_name = normalized_config.get("authorization_env_var")
        if env_name:
            secret = os.environ.get(env_name)
            if not secret or len(secret) > MAX_AUTHORIZATION_VALUE_LENGTH:
                raise ConditionProviderError("authorization_unavailable")
            headers["Authorization"] = f"Bearer {secret}"

        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._request_once(validated_target, headers)
        except ConditionProviderError:
            raise
        except TimeoutError as exc:
            raise ConditionProviderError("timeout") from exc
        except (aiohttp.ClientError, OSError, socket.gaierror) as exc:
            raise ConditionProviderError("network_error") from exc


class ConditionProviderRegistry:
    """Explicit provider registry; adding a provider does not touch reminder logic."""

    def __init__(self, providers: Mapping[str, ConditionProvider] | None = None) -> None:
        self._providers: dict[str, ConditionProvider] = {}
        for provider_type, provider in (providers or {}).items():
            self.register(provider_type, provider)

    def register(self, provider_type: str, provider: ConditionProvider) -> None:
        normalized_type = normalize_provider_type(provider_type)
        self._providers[normalized_type] = provider

    def get(self, provider_type: str) -> ConditionProvider:
        normalized_type = normalize_provider_type(provider_type)
        try:
            return self._providers[normalized_type]
        except KeyError as exc:
            raise ConditionProviderError("unknown_provider") from exc

    def contains(self, provider_type: str) -> bool:
        try:
            return normalize_provider_type(provider_type) in self._providers
        except ConditionProviderError:
            return False

    def types(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


def build_default_condition_provider_registry(
    settings: Any | None = None,
) -> ConditionProviderRegistry:
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    provider = HttpConditionProvider(
        timeout_seconds=settings.condition_request_timeout_seconds,
        max_response_bytes=settings.condition_max_response_bytes,
        authorization_env_allowlist=getattr(settings, "condition_authorization_env_allowlist", ()),
    )
    return ConditionProviderRegistry({"http_json": provider, "http": provider})
