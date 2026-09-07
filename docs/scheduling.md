# Reminder scheduling contract

## Persisted state

- `remind_at_utc` is the canonical UTC instant for the current occurrence. It is not
  overwritten by snooze.
- `schedule_timezone` is captured when the reminder is created. Existing recurring
  reminders keep this timezone if the user changes their profile timezone; a later
  explicit edit can change it.
- `delivery_at_utc` is the next delivery instant. It normally equals
  `remind_at_utc` and temporarily points at a snoozed delivery time.
- `snoozed_until_utc` records the active snooze override, when present.
- `recurrence_day_of_month` preserves the original monthly anchor (for example, 31),
  so a January 31 schedule returns to March 31 after a clamped February occurrence.
- `recurrence_rule` stores the versioned canonical rule as bounded JSON. The rule,
  rather than only the next UTC timestamp, is the source needed to reconstruct and
  edit a series after restart. Legacy scalar recurrence columns remain populated
  for compatibility.

The worker queries the effective delivery time, sends the current occurrence, then
advances the canonical occurrence and resets the delivery override. Each delivery
also has a persisted `ReminderOccurrence` identity. A snooze of an already delivered
recurring occurrence uses a linked one-off child delivery; it never rewrites the
canonical series. This keeps a snooze local to one occurrence and makes the next
occurrence reconstructable after restart from PostgreSQL alone.

## Input semantics

Every parsed reminder carries an explicit datetime semantics:

- `wall_clock` is used for absolute local dates/times and daily, weekly, or monthly
  calendar rules. `to_utc` applies the documented ambiguous/nonexistent-time policy.
- `instant` is used for relative minute/hour input and elapsed minute/hour recurrence.
  The parser first adds the elapsed interval through UTC; `create_reminder` then uses
  the already-aware instant directly. It must not pass that value back through
  wall-clock localization, because doing so could lose `fold=1` during fall-back.

## Calendar arithmetic

Daily, weekly, and monthly recurrence is calculated from the canonical occurrence in
`schedule_timezone` as a local wall-clock/calendar operation. The concrete local
datetime is then converted to UTC for persistence and delivery. Minute and hourly
intervals remain elapsed UTC durations because they represent intervals rather than
calendar wall-clock rules.

## DST policy

- A nonexistent local time during a spring-forward transition is shifted forward by
  the transition gap to the first valid local time.
- An ambiguous local time during a fall-back transition uses the earlier occurrence
  (`fold=0`).
- These policies are deterministic and are applied before UTC persistence.

## Calendar edge cases

- `29 February` is accepted only when the supplied year is a leap year.
- Monthly schedules retain their original day-of-month anchor and clamp only the
  target month: January 31 -> February 28/29 -> March 31.
- A user timezone change does not silently rewrite existing schedules. One-off
  reminders retain their persisted UTC instant; existing recurring reminders retain
  `schedule_timezone`. New reminders use the user's current timezone.
- Missed recurring occurrences advance from the canonical occurrence until the next
  occurrence is in the future; they are not replayed in a burst.
- Because canonical occurrence, schedule timezone, and delivery override are stored,
  a normal worker restart does not require in-memory scheduling state. Recovery of a
  reminder left in `processing` after a worker crash, including lease expiry and retry
  ownership, is defined in the [worker delivery reliability contract](worker_delivery.md)
  for Issue #9.

## User timezone contract

New users default to `Europe/Moscow`. A validated explicit timezone choice is stored in
`users.timezone` and is read back from PostgreSQL on later sessions/restarts. New
reminders capture that currently persisted value in `schedule_timezone`; existing
recurring reminders keep their captured value even if the profile timezone changes.

## Canonical rule model

Version 1 stores a bounded JSON rule alongside the current UTC occurrence:

```json
{"version":1,"kind":"weekly_days","weekdays":[0,3],"interval":1,
 "time":"09:00","anchor_week":"2026-09-07"}
```

`weekly_days` and `weekdays` represent selected weekdays (`0` is Monday),
`monthly_nth` and `monthly_last` represent calendar weekdays, `yearly` represents a
month/day wall-clock date, and `completion_relative` stores the number of calendar
days after the user's actual `Done` action. Optional `until` is an inclusive local
calendar date. Rules are validated and size-bounded before persistence.

Selected weekday sets, workdays, nth/last weekdays of a month, yearly dates, and
repeat-until boundaries use the same local calendar policy. A completion-relative
series keeps its delivered occurrence actionable until `Done`; only then is the next
occurrence calculated from the persisted completion timestamp. Snoozing such an
occurrence keeps the same anchor and schedules the next series occurrence when the
snoozed child is completed.

An inclusive `until` date is terminal: a matching occurrence on that local date is
allowed, and no later occurrence is generated. If the first possible occurrence is
already beyond `until`, creation rejects the dead schedule instead of persisting it.

## Clarification flow

Ambiguous or unsupported reminder text never creates a row. The parser returns a
bounded clarification request, and the bot stores only the owner/chat scope, raw
input, safe category, prompt, and a 15-minute expiry in PostgreSQL. A restart can
resume the prompt; `/cancel` removes it. Invalid replies keep the same prompt, while
an explicit date/time or full command creates the reminder through the normal service
validation path.

For `напомни после обеда ...`, a time-only answer such as `14:00` uses the
user's local calendar: it targets today when that time is still ahead, otherwise
the next local day. Clarification consumption and reminder creation share one
transaction, so duplicate Telegram retries cannot create two reminders.

Existing recurring reminders are migrated deterministically to a version 1 `legacy`
rule from their scalar `recurrence_type`, `recurrence_interval`, and
`recurrence_day_of_month` values. No existing schedule is reinterpreted; the scalar
columns remain available for rollback inspection and compatibility.
