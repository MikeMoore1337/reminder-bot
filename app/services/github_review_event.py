"""Pure, offline GitHub pull-request review-request classifier (#63).

Does not fetch GitHub, enable subscriptions, or deliver Telegram messages.
Only a future trusted, allowlisted GitHub API adapter may supply its inputs.
Never treat this parser as a source-authentication or CI verification layer.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.services.condition_provider import (
    ConditionObservation,
    ConditionProviderError,
    HttpConditionProvider,
    _read_bounded_response,
    validate_https_target,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}")
_REVIEWER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_PULL_TARGET = re.compile(r"([^/#]+/[^/#]+)#([1-9][0-9]{0,8})")
MAX_REVIEWERS = 100


@dataclass(frozen=True, slots=True)
class GitHubReviewTarget:
    repository: str
    pull_number: int


def parse_review_target(
    target: str, *, allowed_repositories: Collection[str]
) -> GitHubReviewTarget:
    """Authorize a named repository and one PR, without accepting URLs or credentials."""
    if type(target) is not str or len(target) > 160:
        raise ConditionProviderError("github_target_invalid")
    match = _PULL_TARGET.fullmatch(target)
    if match is None:
        raise ConditionProviderError("github_target_invalid")
    repository, number = match.groups()
    if _REPOSITORY.fullmatch(repository) is None or ".." in repository:
        raise ConditionProviderError("github_target_invalid")
    if type(allowed_repositories) not in (set, frozenset, tuple, list):
        raise ConditionProviderError("github_repo_allowlist_missing")
    allowlist = {
        item.casefold()
        for item in allowed_repositories
        if type(item) is str and _REPOSITORY.fullmatch(item) is not None
    }
    if repository.casefold() not in allowlist:
        raise ConditionProviderError("github_repo_not_allowed")
    return GitHubReviewTarget(repository=repository, pull_number=int(number))


def classify_review_request(
    payload: object, *, target: GitHubReviewTarget, reviewer_login: str
) -> ConditionObservation:
    """Classify an externally *claimed* GitHub PR snapshot.

    A trusted HTTP adapter must authenticate/fetch the exact GitHub API resource
    and enforce freshness, origin, rate limits, opt-in and delivery filtering.
    """
    if (
        type(reviewer_login) is not str
        or _REVIEWER.fullmatch(reviewer_login) is None
        or type(target) is not GitHubReviewTarget
    ):
        raise ConditionProviderError("github_reviewer_invalid")
    if type(payload) is not dict:
        raise ConditionProviderError("github_response_invalid")
    try:
        base_repo = payload["base"]["repo"]["full_name"]
        number = payload["number"]
        state = payload["state"]
        draft = payload["draft"]
        requested = payload["requested_reviewers"]
    except (KeyError, TypeError, AttributeError):
        raise ConditionProviderError("github_response_invalid") from None
    if (
        type(base_repo) is not str
        or base_repo.casefold() != target.repository.casefold()
        or type(number) is not int
        or number != target.pull_number
    ):
        raise ConditionProviderError("github_identity_mismatch")
    if type(state) is not str or state not in ("open", "closed") or type(draft) is not bool:
        raise ConditionProviderError("github_response_invalid")
    if type(requested) is not list or len(requested) > MAX_REVIEWERS:
        raise ConditionProviderError("github_response_invalid")

    reviewers: set[str] = set()
    for entry in requested:
        if type(entry) is not dict or type(entry.get("login")) is not str:
            raise ConditionProviderError("github_response_invalid")
        login = entry["login"]
        if _REVIEWER.fullmatch(login) is None:
            raise ConditionProviderError("github_response_invalid")
        reviewers.add(login.casefold())

    merged = payload.get("merged_at")
    if merged is not None and (type(merged) is not str or state != "closed"):
        raise ConditionProviderError("github_response_invalid")
    if state == "closed":
        normalized = "resolved"
    elif draft or reviewer_login.casefold() not in reviewers:
        normalized = "inactive"
    else:
        normalized = "action_required"
    return ConditionObservation(state=normalized)


class GitHubReviewProvider(HttpConditionProvider):
    """Opt-in GitHub API reader; no arbitrary target URL, no posting or delivery."""

    provider_type = "github_review"

    def __init__(
        self,
        *,
        allowed_repositories: Collection[str],
        reviewer_login: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not allowed_repositories or not reviewer_login or _REVIEWER.fullmatch(reviewer_login) is None:
            raise ConditionProviderError("github_provider_unconfigured")
        self.allowed_repositories = frozenset(allowed_repositories)
        self.reviewer_login = reviewer_login

    def validate_target(self, target: str) -> GitHubReviewTarget:
        return parse_review_target(target, allowed_repositories=self.allowed_repositories)

    def _api_url(self, target: str) -> str:
        parsed = self.validate_target(target)
        return (
            "https://api.github.com/repos/"
            + parsed.repository
            + "/pulls/"
            + str(parsed.pull_number)
        )

    def validate_config_for_target(
        self, target: str, *, config: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        normalized = super().validate_config_for_target(
            self._api_url(target), config=config
        )
        if "authorization_env_var" not in normalized:
            raise ConditionProviderError("github_authorization_required")
        return normalized

    async def _request_with_session(
        self, session: Any, target: str, headers: Mapping[str, str]
    ) -> ConditionObservation:
        # _request_once() already pins public DNS, enforces TLS/timeout and forbids redirects.
        async with session.get(target, headers=dict(headers), allow_redirects=False) as response:
            code = int(response.status)
            if code == 429 or (code == 403 and response.headers.get("Retry-After")):
                raise ConditionProviderError("rate_limited", status_code=code)
            if code != 200:
                raise ConditionProviderError("github_http_error", status_code=code)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise ConditionProviderError("github_content_type_invalid")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) < 0 or int(length) > self.max_response_bytes:
                        raise ConditionProviderError("response_too_large")
                except ValueError:
                    raise ConditionProviderError("github_content_length_invalid") from None
            raw = await _read_bounded_response(response, self.max_response_bytes)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ConditionProviderError("github_response_invalid") from None
            path = target.removeprefix("https://api.github.com/repos/")
            parts = path.split("/")
            if len(parts) != 4 or parts[2] != "pulls":
                raise ConditionProviderError("github_target_invalid")
            selected = self.validate_target(parts[0] + "/" + parts[1] + "#" + parts[3])
            return classify_review_request(
                body, target=selected, reviewer_login=self.reviewer_login
            )

    async def observe(
        self, target: str, *, config: Mapping[str, Any] | None = None
    ) -> ConditionObservation:
        url = self._api_url(target)
        normalized = self.validate_config_for_target(target, config=config)
        env_name = normalized["authorization_env_var"]
        secret = self.environment.get(env_name)
        if not secret or len(secret) > 4096:
            raise ConditionProviderError("authorization_unavailable")
        validated = validate_https_target(url)
        if validated.hostname != "api.github.com" or validated.port != 443:
            raise ConditionProviderError("github_origin_invalid")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "reminder-bot-condition-provider",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": "Bearer " + secret,
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._request_once(validated, headers)
        except ConditionProviderError:
            raise
        except TimeoutError:
            raise ConditionProviderError("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise ConditionProviderError("network_error") from None
