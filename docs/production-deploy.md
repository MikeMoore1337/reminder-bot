# Production deployment

Repository-side production deployment is intentionally disabled by default.

The deployment workflow is `.github/workflows/deploy-production.yml`. It can run only when repository variable `DEPLOY_ENABLED` is exactly `true` and either:

- the existing `CI` workflow completed successfully for a `push` to `master`; or
- an owner manually dispatches the deploy workflow from `master`, where the workflow re-verifies that the exact SHA already has a successful `CI` push run.

The workflow always deploys an exact 40-character SHA. Both the GitHub runner and the VPS reject a queued/stale deploy when current `origin/master` is no longer that SHA.

## Owner-only bootstrap

Do not perform these steps as part of ordinary repository work. They require explicit owner authorization.

### 1. Prepare a dedicated VPS deployment account

Prefer a dedicated non-root account that can:

- read/write the application checkout;
- run Docker Compose for this application;
- fetch the public GitHub repository;
- not administer unrelated services or containers.

The production checkout should live at one absolute path such as `/opt/reminder-bot`. Bootstrap it once from `MikeMoore1337/reminder-bot` and keep its `origin` pointing to that repository.

The checkout must contain a production `.env` that is ignored by Git and readable only by the deployment account. The workflow never uploads or rewrites this file.

Do not place `mtproxy` or another unrelated Compose project in the Reminder Bot application directory/project.

### 2. Create a dedicated SSH key

Create a deployment-only key pair. Install only the public key in the authorized keys for the deployment account.

Store the private key as GitHub Actions secret:

- `PROD_SSH_PRIVATE_KEY`

Do not reuse a personal/root SSH key when a dedicated key is practical.

### 3. Pin the VPS host key

Obtain the VPS SSH host key fingerprint through a trusted owner-controlled channel and construct the exact OpenSSH `known_hosts` line outside GitHub Actions.

Store the line(s) as GitHub Actions secret:

- `PROD_SSH_KNOWN_HOSTS`

The workflow uses `StrictHostKeyChecking=yes` and deliberately does not run `ssh-keyscan` as trust bootstrap.

### 4. Configure repository variables

Set:

- `PROD_SSH_HOST` - production host/IP;
- `PROD_SSH_PORT` - SSH port;
- `PROD_SSH_USER` - dedicated deployment account;
- `PROD_APP_DIR` - absolute production repository path;
- `DEPLOY_ENABLED` - leave unset or `false` until bootstrap verification is complete.

### 5. Verify the VPS manually before enablement

Before `DEPLOY_ENABLED=true`, owner verification should confirm:

- `git status --porcelain --untracked-files=all` is empty in the production checkout (ignored `.env` is allowed);
- `git remote get-url origin` points to the intended Reminder Bot repository;
- `docker compose config --quiet` succeeds without printing the interpolated configuration;
- PostgreSQL uses the intended persistent volume;
- `docker compose --profile tools run --rm migrate` can reach the production database;
- `bot` and `worker` are the only Reminder Bot application processes;
- unrelated containers such as `mtproxy` are outside this Compose project;
- the dedicated deployment user can perform only the required Git/Docker operations.

Do not run destructive downgrade/volume cleanup as an enablement test.

### 6. Enable automatic deployment

Only after explicit owner approval, set:

`DEPLOY_ENABLED=true`

The next successful `CI` workflow on current `master` will trigger the deploy workflow. A manual dispatch from `master` can re-run deployment only for a SHA that already has a successful `CI` push run.

## Remote deploy sequence

`scripts/deploy_production.sh` fails closed and performs:

1. validate arguments, tools, repository, `.env`, and clean non-ignored worktree;
2. fetch `origin/master` and require exact equality with the requested SHA;
3. detach-checkout the exact SHA;
4. validate Compose quietly;
5. build application images;
6. start PostgreSQL;
7. run `alembic upgrade head` through the `migrate` Compose service;
8. recreate/start `bot` and `worker` without `docker compose down`;
9. poll `/readyz` for up to 60 seconds;
10. require the worker container to be running;
11. print only the deployed SHA.

The deploy path never runs `docker compose down --volumes`, `git clean`, `git reset --hard`, a database downgrade, or commands against unrelated Docker projects.

## Failure and recovery

A failed deploy exits non-zero in GitHub Actions.

- Failure before bot/worker rollout leaves the existing application containers running.
- Migration failure stops the deployment before application rollout. Inspect the migration error and database state before retrying.
- Readiness/worker failure after rollout requires diagnosis on the VPS. Do not delete the PostgreSQL volume to recover.
- A stale queued SHA is rejected. Let the current successful `master` CI trigger a new deployment instead of forcing the older SHA.
- Re-running the deploy workflow manually is allowed only after the exact current master SHA already has a successful `CI` push run.

Rollback of application code may require a normal Git revert merged through protected `master`, followed by green CI and automatic deployment. Database rollback is not automatic and requires an explicit owner-approved migration/recovery plan.

## Secrets and logs

The workflow does not print `.env`, private keys, or known-host content. SSH material exists only in the ephemeral GitHub runner and is removed in an `always()` cleanup step.

Application secrets remain on the VPS. Repository CI and deployment-contract tests require no Telegram or production database credentials.
