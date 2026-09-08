# Condition providers

Condition providers are an opt-in extension point for reminders driven by an
external state. They are deliberately separate from the time-reminder domain
and from `app.workers.reminder_worker`: an unavailable endpoint can only delay
its own subscription and cannot stop delivery of ordinary reminders.

## Contract and lifecycle

`ConditionProvider` receives a target and a safe provider configuration and
returns a normalized `ConditionObservation`:

- `state` is a short, lower-case key (`a-z`, digits, `_`, `.`, `:`, `-`);
- `fingerprint` is optional derived metadata; raw response bodies never cross
  the provider boundary or enter PostgreSQL;
- provider failures use a bounded `ConditionProviderError.code`, optionally
  with a bounded `retry_after_seconds` and HTTP status.

`ConditionProviderRegistry` is the only provider lookup boundary. A new
provider is registered there and does not require changes to reminder parsing,
scheduling, or delivery logic.

Each subscription follows this state machine:

1. an active due row is claimed with a short PostgreSQL transaction and a
   lease token;
2. the token-owned lease is renewed immediately before the external request;
   the condition worker claims one row at a time so queued entries cannot keep
   an expired lease while an earlier provider call is running;
3. success writes one observation, updates the last state, resets failure
   backoff, and schedules the next poll;
4. a first observation establishes a baseline unless `trigger_on_initial` is
   enabled; a later change of `state` creates one transition;
5. the transition and one pending `condition_deliveries` outbox row are
   committed atomically under the subscription lease;
6. failures write a bounded failure observation and exponential retry time;
   another subscription and the time worker continue independently.

`condition_deliveries` is the durable handoff for a future Telegram delivery
adapter. The unique `(subscription_id, transition_sequence)` constraint and
the subscription lease make a repeated poll or restart unable to create a
second trigger for the same transition. The outbox is not silently sent by the
time-reminder worker.

## Reference provider and security policy

The first implementation is `http_json`, a bounded HTTPS JSON provider. Its
payload is an object containing a string `state`. It enforces:

- HTTPS only, no URL userinfo, fragment, or query string, default/443 port
  only, and bounded URL length. Query strings are rejected before the target is
  persisted, including non-obvious credential-bearing parameter names;
- DNS resolution before the request, rejection of every non-global address
  (private, loopback, link-local, multicast, unspecified, reserved), and a
  pinned validated address set for the connection;
- no redirects, an explicit total timeout, an optional `Content-Length` limit,
  bounded chunked-body reads, and JSON content-type validation;
- no raw provider response, URL, exception text, or authorization value in
  PostgreSQL, logs, or user-facing errors;
- optional `authorization_env_var`, which stores only a deployment-owned name
  from `CONDITION_AUTHORIZATION_ENV_ALLOWLIST`. Every allowlisted name must
  also appear in the deployment-owned
  `CONDITION_AUTHORIZATION_ENV_BINDINGS` policy as
  `ENV_NAME=https://approved-origin.example`; the provider rejects a target
  whose normalized HTTPS origin does not match that binding before reading the
  environment. Built-in runtime names such as `BOT_TOKEN`, `DATABASE_URL`,
  `WEBHOOK_SECRET_TOKEN`, and `DEPLOY_ENABLED` are always forbidden. The
  secret is read just before the request and used only in an in-memory
  `Authorization: Bearer` header.

Provider configuration is JSON, but the shared normalizer currently permits
only the authorization environment-variable reference. Provider-specific
configuration must extend this boundary deliberately rather than becoming an
arbitrary header or secret store.

The implementation does not fetch arbitrary HTML, follow redirects, execute
local helpers, or accept live credentials in tests. Generic webhooks and
GitHub PR/Actions providers remain candidates for future Issues: they must
implement the same normalized contract and bounded failure semantics. A
webhook adapter should authenticate and verify a request before writing a
normalized observation; a GitHub adapter should use an allowlisted API origin
and a dedicated token reference, never a user-supplied URL.

## Bounds and operations

Condition polling is disabled by default with `CONDITION_WORKER_ENABLED=false`.
The optional `condition_worker` loop is separate and is not wired into the
existing time worker. Defaults are conservative: batch size 10, five-minute
poll interval, ten bounded drain batches per pass with a one-second drain
cadence when more due work remains, ten-second request timeout, 64 KiB response
limit, 60-second retry base, one-hour retry cap, and a 90-second lease. Observation cleanup runs
at most once per hour by default and retains the last 90 days. Settings
validation requires the lease to exceed the request timeout and the retry cap
to cover the retry base. Cleanup runs in the supervised condition worker on
its own bounded cadence; a cleanup failure is logged as a safe error and does
not stop condition polling or the time worker, with a later cadence retry.

Only bounded counters and provider/type labels should be used for metrics:
poll attempts, successes, failures, timeouts, rate limits, transition count,
deduplicated observations, latency, and current backoff. Never attach raw
payloads, URLs, tokens, or response bodies to metrics or logs.

`cleanup_history` removes only old observation rows. The condition worker calls
it automatically; the public method remains available for controlled
maintenance and tests. Transition and pending outbox records are retained so
the state/trigger audit remains restart-safe; pending delivery rows are never
removed by history cleanup.

No production secret, variable, database write, migration, deploy, volume, or
`mtproxy` operation is performed by this feature. Enabling the optional worker
and provisioning any real provider environment variable remain deployment and
owner-controlled decisions.
