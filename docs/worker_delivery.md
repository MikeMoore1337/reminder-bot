# Worker delivery reliability contract

## Persisted ownership state

The worker owns a reminder only while its row is `processing` and contains a
unique `lease_token`. A claim records:

- `processing_started_at` - when this ownership attempt began;
- `lease_until` - the UTC deadline after which another worker may reclaim it;
- `lease_token` - the ownership generation used by every metadata and finalization update;
- `attempt_count` - the number of claims for the current occurrence;
- `retry_count` - the number of finalized delivery failures for the current occurrence;
- `next_retry_at` - the earliest UTC time for the next transient retry.
- Persistent reminders additionally persist their repeat policy, bounded delivery
  and escalation counters, quiet-hour deferral count, and `delivery_at_utc`. A
  user-row cooldown reservation prevents concurrent workers from sending a burst
  of important reminders to one chat.

`pending` rows are claimable when their effective delivery time and
`next_retry_at` are due. `processing` rows with a missing or expired lease are
also claimable. The PostgreSQL claim uses `FOR UPDATE SKIP LOCKED`, so concurrent
workers cannot claim the same row at the same time.

An update that finalizes a delivery must match the reminder ID, `processing`
status, and exact lease token. A stale worker therefore becomes a no-op after a
lease is reclaimed. Recurring advancement and state transition happen in one
transaction, so a successful occurrence advances at most once.

## Retry and failure policy

The following bounded settings are configurable through environment variables:

- `WORKER_LEASE_DURATION_SECONDS` (1-86400, default 60);
- `WORKER_SEND_TIMEOUT_SECONDS` (1-300, default 30);
- `WORKER_LEASE_SAFETY_MARGIN_SECONDS` (1-300, default 10);
- `WORKER_RETRY_BASE_SECONDS` (1-3600, default 10);
- `WORKER_RETRY_MAX_SECONDS` (1-86400, default 300);
- `WORKER_MAX_ATTEMPTS` (1-20, default 3).

Network errors, timeouts, server errors, `429` responses, and unknown provider
errors are transient. Generic exponential retry delay is capped by
`WORKER_RETRY_MAX_SECONDS`; an explicit Telegram `retry_after` delay is
honored even when it is longer than that generic cap.
Bad requests, blocked/deactivated chats, unauthorized/not-found delivery, and
other explicitly terminal Telegram failures become `failed` without resurrection.
When the claim count reaches `WORKER_MAX_ATTEMPTS`, the occurrence becomes
`failed` even if the worker crashed before it could report an error.

Failure text stored in PostgreSQL and transition logs contain only a bounded
exception type/classification and retry metadata. Reminder text, bot tokens,
raw callback payloads, and provider secrets are never logged.

The lease duration must be strictly greater than the send timeout plus the
safety margin: `WORKER_LEASE_DURATION_SECONDS >
WORKER_SEND_TIMEOUT_SECONDS + WORKER_LEASE_SAFETY_MARGIN_SECONDS`. The
settings object rejects an invalid combination at startup. A batch claim can
contain more rows than can be sent inside one original lease, so every row is
atomically renewed immediately before its Telegram call. The renewal requires
the exact processing token and a still-live lease, commits before the network
call, and holds no database lock during the call. A cancelled, reclaimed, or
expired row is skipped without sending.

## External-send idempotency boundary

Database transitions are idempotent and ownership-guarded:

- duplicate success/failure finalization with the same token is a no-op after the
  first transition;
- stale workers cannot overwrite `last_message_id`, recurrence, or failure state;
- a cancelled, sent, failed, or newer occurrence cannot be resurrected by a stale
  callback;
- a crash after claim is recoverable when the lease expires;
- a crash after Telegram accepts the message but before PostgreSQL finalization
  can result in one duplicate after lease recovery. Telegram send and database
  commit are separate systems, so this at-least-once boundary is unavoidable
  without an external idempotency broker/provider contract and is intentionally
  documented rather than hidden.

`last_delivery_occurrence_utc` binds the stored message ID to the occurrence that
was sent. One-off successful deliveries retain the latest message identity while
in `sent`, so Snooze and Delete callbacks are accepted only for the owning user,
the expected Telegram message ID, and that exact occurrence. Snooze atomically
clears the consumed identity and reschedules the one-off reminder; Delete records
cancellation rather than removing the row. Repeated or stale callbacks therefore
become no-ops. Recurring deliveries may expose occurrence actions: Done is
occurrence-scoped, while Delete cancels the series. A recurring Snooze creates
one linked one-off child for the exact source occurrence, leaving the canonical
series and its worker lease untouched.

## Migration and pre-lease rows

Migration `20260907_0004` adds lease/retry columns and due/retry/lease indexes;
`20260907_0005` adds persisted user-facing states, occurrence identities, and
restart-safe action drafts.
Existing `attempt_count` values are backfilled from the existing `retry_count`
budget. Existing rows in the old token-less `processing` state are then
deterministically reset to `pending` with cleared lease fields, preserving their
canonical delivery time and consumed retry budget.
The old worker must be stopped while this migration runs; this prevents a
pre-migration worker without a token from finalizing a row that the new worker
has reclaimed. After deployment, no manual SQL edit is required for recovery.

## Shutdown and observability

The worker accepts an `asyncio.Event` stop signal. `SIGINT` and `SIGTERM` set the
signal in the worker process. Once set, no new batch is claimed; an item already
being sent is allowed to finish within the configured send timeout, while other
claimed rows retain a bounded lease and are recoverable after restart. Task
cancellation also leaves active rows lease-recoverable.

The worker exposes in-process counters for claimed, recovered, retried, delivered,
failed, and expired leases, plus a processing-age gauge. Structured logs include
only reminder ID, attempt, state/error classification, counts, and processing age.
Persistent policy deferrals and exhausted cycles are reported as bounded counters;
reminder content and secret values are not logged.
