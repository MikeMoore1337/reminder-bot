"""Offline I4 policy: PR owner-review request classification, no network or Telegram."""

from __future__ import annotations

import copy
import json
import socket

import pytest
from aiohttp.abc import AbstractResolver

from app.services.condition_provider import (
    ConditionObservation,
    ConditionProviderError,
    build_default_condition_provider_registry,
)
from app.services.github_review_event import (
    GitHubReviewProvider,
    GitHubReviewTarget,
    classify_review_request,
    parse_review_target,
)

REPOSITORY = "MikeMoore1337/hermes"
ALLOWED = frozenset({REPOSITORY, "MikeMoore1337/your-fitness-coach"})
TARGET = GitHubReviewTarget(repository=REPOSITORY, pull_number=559)
OWNER = "OwnerLogin"


def snapshot(*, requested: bool = True) -> dict[str, object]:
    return {
        "base": {"repo": {"full_name": REPOSITORY}},
        "number": 559,
        "state": "open",
        "draft": False,
        "merged_at": None,
        "requested_reviewers": [{"login": OWNER}] if requested else [],
        "labels": [],
    }


def test_strict_target_allowlist_and_no_url_or_other_projects() -> None:
    assert parse_review_target(f"{REPOSITORY}#559", allowed_repositories=ALLOWED) == TARGET
    for bad in (
        "https://api.github.com/repos/MikeMoore1337/hermes/pulls/559",
        "MikeMoore1337/hermes#0",
        "MikeMoore1337/hermes#-5",
        "MikeMoore1337/hermes#true",
        "MikeMoore1337/hermes#1234567890",
        "MikeMoore1337/hermes#559?token=secret",
        "MikeMoore1337/hermes#559#extra",
        "MikeMoore1337/hermes#559\n",
        "MikeMoore1337/hermes/../../second-brain#5",
        "MikeMoore1337/crypto-ema-bot#8",
        "MikeMoore1337/otus-python#1",
    ):
        with pytest.raises(ConditionProviderError):
            parse_review_target(bad, allowed_repositories=ALLOWED)


def test_missing_allowlist_fails_closed() -> None:
    with pytest.raises(ConditionProviderError, match="github_repo_not_allowed"):
        parse_review_target(f"{REPOSITORY}#559", allowed_repositories=())
    with pytest.raises(ConditionProviderError, match="github_repo_allowlist_missing"):
        parse_review_target(f"{REPOSITORY}#559", allowed_repositories=None)  # type: ignore[arg-type]


def test_request_for_exact_reviewer_is_actionable_without_ci_claims() -> None:
    observation = classify_review_request(snapshot(), target=TARGET, reviewer_login=OWNER)
    assert observation == ConditionObservation(state="action_required")
    assert observation.fingerprint is None
    assert (
        classify_review_request(snapshot(), target=TARGET, reviewer_login="ownerlogin").state
        == "action_required"
    )


def test_not_requested_draft_or_closed_not_actionable() -> None:
    assert (
        classify_review_request(
            snapshot(requested=False), target=TARGET, reviewer_login=OWNER
        ).state
        == "inactive"
    )
    candidate = snapshot()
    candidate["draft"] = True
    assert (
        classify_review_request(candidate, target=TARGET, reviewer_login=OWNER).state == "inactive"
    )
    candidate["draft"] = False
    candidate["state"] = "closed"
    candidate["merged_at"] = "2026-10-24T13:00:00Z"
    assert (
        classify_review_request(candidate, target=TARGET, reviewer_login=OWNER).state == "resolved"
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p.update(number=560), "github_identity_mismatch"),
        (lambda p: p.update(number=True), "github_identity_mismatch"),
        (
            lambda p: p["base"]["repo"].update(full_name="MikeMoore1337/second-brain"),
            "github_identity_mismatch",
        ),
        (lambda p: p.update(state="pending"), "github_response_invalid"),
        (lambda p: p.update(state=None), "github_response_invalid"),
        (lambda p: p.update(draft=1), "github_response_invalid"),
        (lambda p: p.pop("requested_reviewers"), "github_response_invalid"),
        (lambda p: p.update(requested_reviewers="OwnerLogin"), "github_response_invalid"),
        (lambda p: p.pop("labels"), "github_response_invalid"),
        (lambda p: p.update(labels=[{"name": 123}]), "github_response_invalid"),
        (lambda p: p.update(requested_reviewers=[{"other": OWNER}]), "github_response_invalid"),
        (lambda p: p.update(requested_reviewers=[{"login": "!"}]), "github_response_invalid"),
        (
            lambda p: p.update(requested_reviewers=[{"login": OWNER}] * 101),
            "github_response_invalid",
        ),
        (lambda p: p.update(merged_at="now"), "github_response_invalid"),
        (lambda p: p.update(merged_at=1), "github_response_invalid"),
    ],
)
def test_untrusted_shape_or_identity_fails_closed(mutation, reason: str) -> None:
    candidate = copy.deepcopy(snapshot())
    mutation(candidate)
    with pytest.raises(ConditionProviderError, match=reason):
        classify_review_request(candidate, target=TARGET, reviewer_login=OWNER)


def test_owner_action_label_works_even_for_pr_author_who_cannot_self_review() -> None:
    candidate = snapshot(requested=False)
    candidate["labels"] = [{"name": "owner-action-required"}]
    assert (
        classify_review_request(candidate, target=TARGET, reviewer_login=OWNER).state
        == "action_required"
    )


def test_invalid_reviewer_and_payload_does_not_leak_content() -> None:
    for reviewer in ("", "owner@", "user name", "a" * 40):
        with pytest.raises(ConditionProviderError, match="github_reviewer_invalid"):
            classify_review_request(snapshot(), target=TARGET, reviewer_login=reviewer)
    with pytest.raises(ConditionProviderError, match="github_response_invalid") as exc:
        classify_review_request({"secret": "LEAK_ME"}, target=TARGET, reviewer_login=OWNER)
    assert "LEAK_ME" not in str(exc.value)


def test_fingerprint_and_state_are_only_safe_labels() -> None:
    observation = classify_review_request(snapshot(), target=TARGET, reviewer_login=OWNER)
    assert observation.state == "action_required"
    assert observation.fingerprint is None
    assert not hasattr(observation, "url")


class _Resolver(AbstractResolver):
    async def resolve(self, host: str, port: int, family: int = socket.AF_INET):
        assert host == "api.github.com"
        return [{"hostname": host, "host": "93.184.216.34", "port": port,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self):
        return None


class _Response:
    def __init__(self, status: int, payload: object):
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = json.dumps(payload).encode()
        self.content = self

    async def read(self, count: int = -1):
        data, self._body = self._body[:count] if count >= 0 else self._body, self._body[count:] if count >= 0 else b""
        return data


class _Context:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    def __init__(self, response, collected):
        self.response = response
        self.collected = collected

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.collected["url"] = url
        self.collected["headers"] = kwargs["headers"]
        self.collected["allow_redirects"] = kwargs["allow_redirects"]
        return _Context(self.response)


def _github_provider(payload, *, status=200, token="local-fixture"):
    collected = {}
    response = _Response(status, payload)
    provider = GitHubReviewProvider(
        allowed_repositories=ALLOWED,
        reviewer_login=OWNER,
        authorization_env_allowlist={"CONDITION_TEST_GITHUB"},
        authorization_env_bindings={"CONDITION_TEST_GITHUB": "https://api.github.com"},
        environment={"CONDITION_TEST_GITHUB": token},
        resolver=_Resolver(),
        session_factory=lambda **kwargs: _Session(response, collected),
    )
    return provider, collected


@pytest.mark.asyncio
async def test_authenticated_bounded_github_api_fetch() -> None:
    provider, captured = _github_provider(snapshot())
    observation = await provider.observe(
        REPOSITORY + "#559",
        config={"authorization_env_var": "CONDITION_TEST_GITHUB"},
    )
    assert observation.state == "action_required"
    assert captured["url"] == "https://api.github.com/repos/MikeMoore1337/hermes/pulls/559"
    assert captured["allow_redirects"] is False
    assert captured["headers"]["Authorization"] == "Bearer local-fixture"
    assert captured["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


@pytest.mark.asyncio
async def test_github_provider_requires_bound_secret_and_repo() -> None:
    provider, captured = _github_provider(snapshot())
    with pytest.raises(ConditionProviderError, match="github_authorization_required"):
        await provider.observe(REPOSITORY + "#559")
    with pytest.raises(ConditionProviderError, match="github_repo_not_allowed"):
        await provider.observe("SomeoneElse/private#1", config={"authorization_env_var": "CONDITION_TEST_GITHUB"})
    with pytest.raises(ConditionProviderError, match="authorization_not_allowed"):
        await provider.observe(REPOSITORY + "#559", config={"authorization_env_var": "BOT_TOKEN"})
    assert captured == {}


@pytest.mark.asyncio
async def test_github_provider_fails_closed_on_untrusted_response() -> None:
    for payload, code in (
        ({"message": "Needs authentication"}, "github_http_error"),
        ({"some": "secret"}, "github_response_invalid"),
        ({**snapshot(), "number": 999}, "github_identity_mismatch"),
    ):
        status = 401 if "message" in payload else 200
        provider, _ = _github_provider(payload, status=status)
        with pytest.raises(ConditionProviderError, match=code):
            await provider.observe(REPOSITORY + "#559", config={"authorization_env_var": "CONDITION_TEST_GITHUB"})


def test_registry_disabled_without_explicit_opt_in() -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(
        condition_request_timeout_seconds=10,
        condition_max_response_bytes=65536,
        condition_authorization_env_allowlist=(),
        condition_authorization_env_bindings={},
        condition_github_review_enabled=False,
    )
    assert not build_default_condition_provider_registry(settings).contains("github_review")
    settings.condition_github_review_enabled = True
    settings.condition_github_allowed_repositories_raw = REPOSITORY
    settings.condition_github_reviewer_login = OWNER
    assert build_default_condition_provider_registry(settings).contains("github_review")
