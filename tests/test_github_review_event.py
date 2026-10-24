"""Offline I4 policy: PR owner-review request classification, no network or Telegram."""

from __future__ import annotations

import copy

import pytest

from app.services.condition_provider import ConditionObservation, ConditionProviderError
from app.services.github_review_event import (
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
