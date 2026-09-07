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

The worker queries the effective delivery time, sends the current occurrence, then
advances the canonical occurrence and resets the delivery override. This keeps a
snooze local to one occurrence and makes the next occurrence reconstructable after
restart from PostgreSQL alone.

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
  ownership, remains the separate delivery-reliability scope of Issue #9.

## User timezone contract

New users default to `Europe/Moscow`. A validated explicit timezone choice is stored in
`users.timezone` and is read back from PostgreSQL on later sessions/restarts. New
reminders capture that currently persisted value in `schedule_timezone`; existing
recurring reminders keep their captured value even if the profile timezone changes.
