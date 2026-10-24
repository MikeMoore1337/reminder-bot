# GitHub owner-action notifications (I4, #63)

## What is implemented

This is an **opt-in** GitHub pull-request condition provider and a Telegram sender
for its durable existing `condition_deliveries` outbox. It runs under the
**existing reminder worker**, with no new service, scheduler, table, LLM or paid API.

A selected, allowlisted PR becomes `action_required` only if it is open, not
a draft and either (1) explicitly requests a review from the configured
GitHub account or (2) contains the `owner-action-required` label. The label
is useful for PR authors, who cannot review their own GitHub PR. **This state
is not a claim that CI has passed**. A closed PR is `resolved`. Missing,
invalid, inaccessible, or mismatched GitHub data is an error with retry,
not a fabricated ready status.

The provider builds one fixed GitHub API resource from `owner/repo#PR`.
No arbitrary target URL or query parameters are accepted. Only the configured
repository allowlist is allowed. The existing hardened HTTPS provider enforces
public DNS, pinned resolved IPs, TLS, no redirects, time/response limits,
and non-sensitive error reporting. GitHub API access **requires** a separate
deployment-owned `Authorization` env reference bound to
`https://api.github.com`. No credential is stored in the DB.

The existing condition service records transitions and a durable outbox.
For the GitHub source it enqueues **only entries into `action_required`**,
not a later removal/closure of the review requirement. The sender claims
rows transactionally, fences attempts, reclaims expired processing leases,
suppresses withdrawn actions, retries bounded transient errors and never
touches `http_json` deliveries.

## Disabled by default

All switches default to false:

- `CONDITION_WORKER_ENABLED`
- `CONDITION_GITHUB_REVIEW_ENABLED`
- `CONDITION_GITHUB_DELIVERY_ENABLED`

Provisioning requires deployment-owned repo allowlist, reviewer login,
a dedicated read-only GitHub credential, the existing
`CONDITION_AUTHORIZATION_ENV_ALLOWLIST` and
`CONDITION_AUTHORIZATION_ENV_BINDINGS` with the approved origin.
Do **not** copy real tokens into Issues, PRs, command output or logs.

A separate authenticated, owner-approved subscription is still needed, for
example through the existing internal `ConditionService.create_subscription`
with `provider_type="github_review"`,
`target="MikeMoore1337/hermes#559"`, nonsecret
`config={"authorization_env_var": "DEPLOYMENT_MANAGED_GITHUB_TOKEN_NAME"}` and
the actual owner DB identity/chat ID. New deployments do not create any
subscriptions by themselves. Setting flags alone is **not an opt-in**.

## Scope and caveats

- Production activation, credentials, owner subscription, destination approval,
  and live Telegram message are **NOT_VERIFIED** until authorized and observed.
- The source is polled on the approved interval; no GitHub event webhook.
  A review request that is added and removed before observation may be missed.
- The outbound Telegram API **does not provide an idempotency key**. The
  transactional outbox avoids duplicate rows and usual repeat sends, but an
  ambiguous network acknowledgment or crash after remote acceptance can
  create a duplicate. Exactly-once remote delivery is **not guaranteed**.
- User quiet hours, notifications for other condition types, per-user Telegram
  command setup, GitHub required-CI evaluation and a production smoke test
  are **not part of this implementation**. No automated assertion of green CI.
- No original folders, personal vault, YFC data, browser logins, bot
  production secrets, paid AI or live environment was accessed while
  implementing this PR.

## Verification

PR: Run all required `quality`, `tests-postgres`, `docker-smoke`
checks on its exact head. Focused tests:

- `python -m pytest -q tests/test_github_review_event.py`
- `python -m pytest -q tests/test_condition_delivery_worker.py`
- `python -m pytest -q tests/test_condition_service.py`

The source-only merge must **not** enable any of the three flags. Real Telegram
opt-in needs separate owner confirmation and an approved production dry-run and
rollback plan.
