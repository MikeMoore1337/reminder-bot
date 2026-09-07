# Reminder Bot repository instructions

These instructions apply to all work in this repository.

## Scope and backlog-only guard

- Treat PostgreSQL as the source of truth.
- Keep the bot and reminder worker as separate processes.
- Do not implement product backlog Issues unless the current task explicitly names the Issue to implement.
- During backlog-only work, do not create feature branches or worktrees for future Issues and do not open a product implementation PR.
- Do not infer product scope from the existence of a skill. Use only the surfaces named by the current Issue.

## Architecture invariants

- Keep domain logic out of Telegram handlers; handlers should translate Telegram input/output and call testable services.
- Keep infrastructure adapters replaceable and testable.
- Do not add Redis, Kafka, RabbitMQ, Celery, Kubernetes, or microservices without a measured need and explicit approval.
- Do not perform a Clean Architecture or DDD rewrite for aesthetic reasons.
- Keep PostgreSQL, SQLAlchemy, and Alembic as the persistence path unless an Issue explicitly changes that decision.

## Reliability invariants

Correctness is more important than feature count. Product work must account for:

- timezone and DST semantics;
- calendar recurrence and missed occurrences;
- delivery state, retries, crash recovery, and idempotency;
- duplicate and stale callbacks;
- restart-safe state and concurrent workers.

## Telegram skill routing

`telegram-engineer/SKILL.md` is the canonical Telegram-specific skill for this project when it is available. Use it only for Telegram-specific behavior affected by the current Issue, including Bot API, Aiogram routers/Dispatcher, polling/webhook ownership, callbacks, FSM flows, media/files, retries, idempotency, rate limits, and Telegram security boundaries.

Use `telegram-engineer` only for Telegram-specific behavior affected by the current task. Ignore TMA, channels, moderation, broadcasts, and publishing sections unless the current Issue explicitly touches those surfaces.

Do not claim that a skill is installed or available unless its `SKILL.md` can be read in the current environment. If the canonical skill or an optional companion is unavailable, continue with ordinary engineering practice and this file; do not install an unreviewed external skill.

Telegram-specific invariants from the canonical skill:

- One bot token has one long-polling owner. The worker may use Bot API delivery, but it must not start a second polling runtime.
- Polling/webhook ownership, allowed updates, startup/shutdown, and retry behavior must be explicit and idempotent.
- Keep canonical commands in one definition and reuse them for runtime registration, help, and tests; command visibility is not authorization.
- Keep multi-step Telegram flows bounded with explicit states, `/cancel`, TTL, stale-state handling, and a restart policy. Do not rely on in-memory state when losing it would lose a critical draft/action.
- Treat callback data as untrusted. Security-sensitive actions bind actor, resource/user scope, immutable revision/version, action, and expiry/stale-state policy; transitions are server-side and idempotent.
- Telegram media uses an allowlist, size/duration/time limits, safe failure, and cleanup. Prefer Bot API references/copy semantics over unnecessary downloads.
- Do not log bot tokens, provider secrets, raw Telegram init data, private message text, documents, photos, or unnecessary identifiers. Live BotFather/channel/production smoke remains manual unless explicitly authorized.

## Companion skill routing

Use only the smallest applicable set, and list the actual skills used in the task report:

- `backend-engineer`: Python domain/services, SQLAlchemy/PostgreSQL, transactions, concurrency, workers, scheduling, and Alembic.
- `qa-engineer`: regression strategy, parser/timezone/DST, worker crash/recovery, retries, callbacks, migration, and integration tests.
- `platform-engineer`: Docker/Compose, process model, health/readiness, shutdown, packaging, resource limits, deployment, and STT runtime.
- `observability-engineer`: stuck jobs, delivery/retry/queue health, worker/STT latency, and safe structured logging.
- `security-engineer`: webhook security, external HTTP, uploads, arbitrary URLs, authorization, condition providers, shared reminders, and admin actions.
- `privacy-engineer`: voice/media, forwarded context, retention, sensitive logs, and stored user data.
- `llm-engineer`: not part of the baseline; use only for a separately approved optional LLM fallback Issue.
- `mobile-engineer`: not part of the baseline; use only for a separately approved Telegram Mini App decision/implementation Issue.

Never add all companion skills to an Issue by default. Missing companion skills are not blockers unless the task explicitly requires their unavailable guidance.

## Delivery workflow

For an approved product task, follow:

`Issue -> feature branch/worktree -> implementation -> tests -> PR -> review -> merge -> cleanup`

- One Issue is one coherent functional goal.
- Prefer one feature branch and one PR per Issue.
- Use `master` as the base branch.
- Codex does not merge a PR unless the owner explicitly asks for that action.
- Refresh base and rerun the relevant final checks after a rebase, amend, or changed commit.

## VPS constraints

Target a small Linux CPU-only VPS with limited RAM:

- use Docker Compose and a small number of continuously running processes;
- do not require a GPU;
- do not add infrastructure with material RAM/CPU cost without evidence of benefit;
- keep heavy optional runtimes out of the baseline deployment when possible.

## Cleanup and secret safety

- Remove temporary development/debug files, caches, test media, and build artifacts created by the task when they are no longer needed.
- Never commit `.env`, secrets, STT models, voice/audio dumps, runtime logs, database dumps, caches, or IDE-only files.
- Preserve `master`, user branches, active branches, unmerged PR branches, unknown files/directories, and persistent runtime data.
- Before removing a task-created worktree or branch, verify that it is task-created and no longer active or needed.
- After a product PR/merge lifecycle, run `git worktree prune` and `git fetch origin --prune`, then report retained artifacts and cleanup status.

## Validation baseline

Before reporting completion, inspect the actual code and run the narrowest relevant checks. Do not treat README claims as authoritative when they differ from code. For backlog-only work, validation must prove that no product implementation, feature branch, worktree, or product PR was created.
