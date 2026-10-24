"""Pure, offline GitHub pull-request review-request classifier (#63).

Does not fetch GitHub, enable subscriptions, or deliver Telegram messages.
Only a future trusted, allowlisted GitHub API adapter may supply its inputs.
Never treat this parser as a source-authentication or CI verification layer.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from app.services.condition_provider import ConditionObservation, ConditionProviderError

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
