# I4 - GitHub review-request observations (offline groundwork)

Tracking: [reminder-bot #63](https://github.com/MikeMoore1337/reminder-bot/issues/63), [Hermes integration #555](https://github.com/MikeMoore1337/hermes/issues/555).

## One actionable event

Selected event: a specific approved GitHub pull request **explicitly requests review from the configured account**. A review request is a factual GitHub PR field, **not** proof that CI passed or that the application reached production.

A future trusted GitHub API adapter may read a pinned HTTPS API endpoint for exactly one repository/pull request. `classify_review_request` only transforms an already obtained response:

- `action_required`: PR is open, not a draft, and the named account appears in `requested_reviewers`;
- `inactive`: no matching request or PR is draft;
- `resolved`: PR is closed;
- invalid/missing source identity or partial response: refusal, **not** `inactive`.

A target is a repository and PR number, e.g. `MikeMoore1337/hermes#559`. `parse_review_target` refuses targets not in an explicit allowlist, arbitrary URLs, unexpected scopes and malformed numbers. It does not fetch the network or read secrets.

## Important limitations

The current addition is a **pure read-only classifier and tests**, not a registered provider, scheduled fetcher or Telegram delivery:

1. No new provider is registered in `ConditionProviderRegistry`.
2. Condition processing is still off by default (`CONDITION_WORKER_ENABLED=false`).
3. Existing `ConditionService` persists transitions and queues `condition_deliveries`, but the queue does not itself send Telegram messages. Do not claim end-to-end notification until an authorized delivery adapter exists.
4. Every transition creates a delivery record under generic semantics. For an owner-only action notification, a future provider/transition policy must emit **only** a transition into `action_required`, not a resolution/inactive event. Do not turn on this scenario before that filter exists.
5. No account/repo authentication, authenticated GitHub HTTP polling, freshness or rate-limit policy, reviewer opt-in, destination control, or production subscriber is implemented here. An offline JSON fixture can be spoofed; trust must come from the network adapter rather than payload fields.
6. The existing time-reminder worker remains independent. No bot token, user profile, Second Brain content, YFC fitness record, infrastructure credential or production environment was accessed.

## Future implementation gates (same Issue)

- An explicit opt-in subscription scoped to one approved repo, one PR, one user account and one Telegram destination.
- HTTPS `api.github.com` fixed-origin read with safe authorization, bounded time/body/rate/backoff, exact repo/PR identity validation, no arbitrary URLs, and no stale/unknown interpreted as healthy.
- Transition filtering (alert on **entry** to `action_required` only) and delivery adapter reusing existing `condition_deliveries`, with documented at-least-once Telegram semantics, retry, duplicate suppression and crash/restart tests.
- Three mandatory CI jobs on exact PR head and owner-approved live verification. A PR merge alone is **not** permission to enable a worker, provision a token or send notifications.
