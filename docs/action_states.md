# Reminder action/state contract

`Reminder.status` remains the internal worker lifecycle (`pending`, `processing`,
`sent`, `failed`). The user-facing state is persisted separately in
`Reminder.state`; `processing` is never rendered as a product state.

## State/action matrix

| User state | Meaning | Actions shown |
| --- | --- | --- |
| `scheduled` | The next canonical occurrence is waiting for delivery. | Snooze, Edit, Pause (recurring), Delete |
| `delivered` | A one-off or snoozed delivery was sent and awaits acknowledgement. | Done, Snooze, Edit, Pause (recurring), Delete |
| `snoozed` | Delivery is scheduled for a later instant. | Snooze, Edit, Pause (recurring), Delete |
| `paused` | A recurring series is stopped at its persisted canonical anchor. | Resume, Edit, Delete |
| `completed` | A one-off reminder, or one recurring occurrence, was acknowledged. | None |
| `cancelled` | The reminder/child delivery was cancelled and retained for audit. | None |
| `failed` | Bounded delivery attempts ended in a terminal failure. | None |

For a recurring reminder, `Done` changes only the delivered occurrence. The
canonical series remains scheduled. `Delete` cancels the canonical series and
its active snooze children; deleting a snooze child cancels only that child and
its source occurrence while the parent series continues.

After a recurring send, the parent row is already advanced to its next
canonical occurrence and therefore remains `scheduled`. While the latest
occurrence is still `delivered`, `/list` renders that occurrence as the action
card; acknowledging it does not stop the next canonical delivery.

## Persisted identity and revisions

`ReminderOccurrence` is the immutable identity boundary for a delivered
occurrence. It stores the canonical occurrence instant, scheduled delivery
instant, delivered timestamp, message ID, occurrence state, and action
revision. The worker creates or reopens it under the valid PostgreSQL lease
before calling Telegram. A successful send records the Telegram message ID
under the same lease.

New callback data uses the compact versioned format:

```text
r1:<action>:<target kind>:<target id>:<revision>
```

`target kind` is `r` for a reminder card from `/list` or `o` for a persisted
occurrence. The server checks owner, chat, resource, exact revision, current
state, and (for occurrence actions) the callback message ID. Legacy
`reminder:*` payloads are rejected as stale and are never applied to a newer
revision.

Every successful state-changing action increments the relevant reminder and/or
occurrence revision. The old Telegram keyboard is removed when Telegram still
allows the edit.

## Snooze and recurrence

`Reminder.remind_at_utc` remains the canonical occurrence. A scheduled snooze
may use `Reminder.delivery_at_utc` as its delivery override. A snooze of an
already delivered recurring occurrence instead marks that occurrence as
`snoozed` and creates one linked one-off `Reminder` child with
`parent_reminder_id` and `source_occurrence_at_utc`. A unique constraint allows
only one child for a parent/source occurrence. The child is independently
leased and survives restart; it cannot mutate the canonical series.

Evening means the next 20:00 in the persisted user timezone. Tomorrow means
09:00 on the next calendar day in that timezone. Both use the scheduling
contract's DST policy. New users still default to `Europe/Moscow`.

## Persistent drafts

Edit and custom Snooze use `ActionDraft`, which stores only owner/chat scope,
action type, reminder ID, expected revision/message/occurrence ID and timestamp,
current step, minimal JSON payload, timestamps, and expiry. Drafts are
restart-safe, expire after a bounded TTL, cannot cross users/chats, and are
cancelled by `/cancel` without storing a Telegram update or private message
history.

## Cancellation and retention

`Delete` and `/cancel ID` perform an atomic persisted transition to
`cancelled`, set `cancelled_at`, increment the revision, and retain the row.
The worker's claim and finalization predicates exclude cancelled state. Physical
purge/retention is outside this Issue.
