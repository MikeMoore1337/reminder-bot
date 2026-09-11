# Production deployment

Repository-side production deployment is disabled by default. No production
SSH, bootstrap, secret/variable change, database mutation, or `DEPLOY_ENABLED`
change is part of the repository implementation.

The deployment workflow is `.github/workflows/deploy-production.yml`. Its only
automatic entry point is a successful `CI` `push` run on `master`. Manual
dispatch is allowed only from `master` and still requires a successful `CI`
`push` run for the exact SHA. Both paths require repository variable
`DEPLOY_ENABLED` to equal the literal string `true`.

## Production facts

The owner-provided production target is Ubuntu Linux x86_64 at
`111.88.215.204`, SSH port `25566`, with host fingerprint
`SHA256:4GKTOEHN6g/j5P8qCdTX1KNXuGoKMG0qN4cod3DFwYY`, Docker `29.6.2`,
Compose `5.3.1`, about 1.9 GiB RAM, 2 GiB swap, and about 24 GiB free disk.
The unrelated `mtproxy` Compose project at `/opt/mtproxy/docker-compose.yml`
is outside this deployment and must never be touched.

`/root/reminder_bot` is a legacy non-canonical directory containing the
production `.env`. It is not used as the deployment checkout and must not be
deleted, renamed, or converted in place. The canonical clean Git checkout is
`/opt/reminder-bot`.

## Production `.env` invariant

The canonical production file `/opt/reminder-bot/.env` must be a regular,
non-symlink file with:

- owner `reminder-deploy`;
- group `reminder-deploy`;
- mode `0600`.

The deploy script compares the file's numeric uid/gid with the effective uid/gid
of the deployment process, so the check does not depend on name resolution. It
then checks the exact mode and opens the file for reading as the deployment
account. Only after this metadata/readability preflight passes does it parse
`BOT_MODE`, and only later does it acquire the deployment lock. Every failure
stops the deploy; the script never prints `.env` contents and never performs
automatic `chown`/`chmod` on `.env`, ACL changes, `sudo`, or file replacement.

This is the fail-fast phase. Immediately after the deployment lock is acquired,
the script repeats the regular-file, ownership, mode, readability, and
`BOT_MODE=polling` checks as the authoritative phase. It then pins a private
shell-only fingerprint containing device/inode, uid/gid, mode, and a SHA-256
content digest. The fingerprint is checked before and after every subsequent
Docker Compose phase, so pathname replacement, inode/metadata changes, or
content changes fail closed with `production .env changed during deployment`.
The digest is never printed and no digest temporary file is created.

## Immutable image transport

The GitHub runner checks out the exact 40-character protected `master` SHA and
builds exactly one application image:

`reminder-bot:<FULL_SHA>`

The image has these OCI labels:

- `org.opencontainers.image.revision=<FULL_SHA>`;
- `org.opencontainers.image.source=https://github.com/MikeMoore1337/reminder-bot`.

The runner sends that image directly to the VPS with `docker save`, a gzip
stream, and the already host-pinned SSH connection. The VPS runs `docker load`
and verifies both labels before changing any Reminder Bot service. No Docker
Hub/GHCR repository or permanent VPS registry credential is required.

`migrate`, `bot`, and `worker` all use the same Compose image expression:

`REMINDER_BOT_IMAGE` (local fallback: `reminder-bot:local`)

Local `docker compose build` remains supported. The production script never
builds on the VPS; every build-capable production `up` operation uses
`--no-build`.

## Stable production identity

Every production Compose command is invoked with project `reminder_bot`. The
Compose volume declaration names the existing PostgreSQL volume explicitly:

`reminder_bot_postgres_data`

The logical Compose volume name remains `postgres_data`. Before the first live
service operation the script requires that the volume already exists, has
labels `com.docker.compose.project=reminder_bot` and
`com.docker.compose.volume=postgres_data`, and contains `PG_VERSION` equal to
`17`. Missing volume, unexpected labels, or another PostgreSQL major version
is a human-required stop; Compose is never allowed to create a replacement
database.

Production polling requires `BOT_MODE=polling` and one `bot` service. The host
port is published only on `127.0.0.1:8080`; the container still listens on
`APP_HOST:APP_PORT`, and local/webhook setups can set `APP_PUBLISH_HOST` in
their own `.env`.

## Owner-only bootstrap procedure

Run this procedure only after the repository PR lifecycle is complete and the
owner explicitly authorizes VPS bootstrap. It is intentionally not automated
by GitHub Actions and has not been run by Codex.

The known legacy directory `/root/reminder_bot` and its `.env` must remain
untouched. Never run a Docker cleanup command, `down` with volume removal, or
any command naming the unrelated `mtproxy` Compose project.

### 1. Create the dedicated account, clone, and then runtime directories

This is a first-bootstrap procedure. The dedicated non-root account uses a
functional `/bin/bash` login shell because OpenSSH executes a forced command
through the account's login shell with `-c`. That does not provide interactive
SSH: the password is locked, the CI key is forced/restricted, and the
account-specific SSH policy below disables interactive features.

The canonical checkout is `/opt/reminder-bot`. The target must be absent or an
empty directory. An existing Git checkout, symlink, file, or non-empty
directory is an unknown deployment state and requires `HUMAN_REQUIRED`; never
delete or replace it. Runtime directories are deliberately created only after
the clone succeeds:

```bash
if ! getent passwd reminder-deploy >/dev/null; then
  sudo useradd --system --create-home --shell /bin/bash reminder-deploy
else
  current_shell="$(getent passwd reminder-deploy | awk -F: '{print $7}')"
  case "${current_shell}" in
    /bin/bash|/bin/sh) ;;
    *) sudo usermod --shell /bin/bash reminder-deploy ;;
  esac
fi
sudo passwd --lock reminder-deploy
case "$(getent passwd reminder-deploy | awk -F: '{print $7}')" in
  /bin/bash|/bin/sh) ;;
  *) printf 'HUMAN_REQUIRED: reminder-deploy must use /bin/bash or /bin/sh.\n' >&2; exit 1 ;;
esac

app_dir=/opt/reminder-bot
if sudo test -e "${app_dir}" || sudo test -L "${app_dir}"; then
  if sudo test -L "${app_dir}" || ! sudo test -d "${app_dir}"; then
    printf 'HUMAN_REQUIRED: %s is not a directory; do not replace it.\n' "${app_dir}" >&2
    exit 1
  fi
  if sudo test -e "${app_dir}/.git" || \
    [ -n "$(sudo find "${app_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    printf 'HUMAN_REQUIRED: %s is non-empty or already a Git checkout; do not replace it.\n' \
      "${app_dir}" >&2
    exit 1
  fi
else
  sudo install -d -o reminder-deploy -g reminder-deploy -m 0755 "${app_dir}"
fi
sudo chown reminder-deploy:reminder-deploy "${app_dir}"
sudo chmod 0755 "${app_dir}"

sudo -u reminder-deploy git clone --branch master --single-branch \
  https://github.com/MikeMoore1337/reminder-bot.git "${app_dir}"
test -d "${app_dir}/.git"
test "$(sudo -u reminder-deploy git -C "${app_dir}" remote get-url origin)" \
  = "https://github.com/MikeMoore1337/reminder-bot.git"

sudo install -d -o reminder-deploy -g reminder-deploy -m 0700 \
  "${app_dir}/backups" "${app_dir}/locks" "${app_dir}/state"
```

Do not use `git reset --hard`, `git clean`, or a broad cleanup to repair a
partial checkout. If the clone fails, stop and investigate before creating
runtime state or copying `.env`.

### 2. Preserve and copy the production environment securely

Do not print either file. Keep the legacy source as-is and copy it only when
the canonical destination has not already been provisioned:

```bash
sudo test -f /root/reminder_bot/.env
if ! sudo test -e /opt/reminder-bot/.env; then
  sudo install -o reminder-deploy -g reminder-deploy -m 0600 \
    /root/reminder_bot/.env /opt/reminder-bot/.env
fi
sudo chown reminder-deploy:reminder-deploy /opt/reminder-bot/.env
sudo chmod 0600 /opt/reminder-bot/.env
```

The file must contain `BOT_MODE=polling` and the existing database settings.
Do not replace the database URL with a new database, and do not delete
`/root/reminder_bot`.

### 3. Install the restricted SSH command boundary

Install the reviewed wrapper as a root-owned file. Create
`/etc/reminder-bot/deploy-app-dir` as a root-owned mode `0644` file containing
exactly one line, `/opt/reminder-bot`, and no trailing configuration:

```bash
sudo install -d -o root -g root -m 0755 /etc/reminder-bot
sudo install -o root -g root -m 0755 \
  /opt/reminder-bot/deploy/scripts/reminder_bot_ssh_wrapper.sh \
  /usr/local/sbin/reminder-bot-ssh-wrapper
sudo chown root:root /etc/reminder-bot/deploy-app-dir
sudo chmod 0644 /etc/reminder-bot/deploy-app-dir
```

The file contents should be reviewed with an owner-controlled editor rather
than printed into a log. The value must match the repository variable
`PROD_APP_DIR`. If a different canonical absolute path is approved, update
the policy file and workflow variable together and review the wrapper allowlist
before enabling deployment.

Generate a new deployment-only ED25519 key on the owner-controlled workstation.
Install only its public key for `reminder-deploy`, using the wrapper and
OpenSSH `restrict` option. The authorized-keys entry has this shape; replace
`<PUBLIC_KEY>` with the generated public key and do not put the private key in
the repository:

```text
command="/usr/local/sbin/reminder-bot-ssh-wrapper",restrict ssh-ed25519 <PUBLIC_KEY> reminder-bot-ci
```

`restrict` disables agent forwarding, TCP forwarding, X11 forwarding, and
PTY allocation. The wrapper accepts only `docker load` and the exact
SHA-carrying deploy command emitted by the workflow; it rejects interactive
shells and arbitrary commands. The Docker group is root-equivalent, so the
forced-command boundary is required if the account is added to it:

```bash
sudo usermod -aG docker reminder-deploy
```

Apply account-specific SSH daemon hardening through the owner’s normal
configuration management, then validate before reload:

```text
Match User reminder-deploy
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AuthenticationMethods publickey
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
```

Run `sudo sshd -t` before reloading the SSH service. Do not reuse an owner or
root key. The workflow also sends
`PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`,
`ForwardAgent=no`, `ForwardX11=no`, `ClearAllForwardings=yes`, and
`RequestTTY=no`, and keeps `StrictHostKeyChecking=yes` with the owner-pinned
`PROD_SSH_KNOWN_HOSTS` value. It never runs `ssh-keyscan`.

### 4. Verify tools and the existing PostgreSQL volume without starting services

Run these checks as the deployment account where appropriate. They may inspect
the existing volume but must not start Compose or create a new volume:

```bash
sudo -u reminder-deploy git --version
sudo -u reminder-deploy docker version
sudo -u reminder-deploy docker compose version
sudo -u reminder-deploy flock --version

volume_metadata="$(sudo -u reminder-deploy docker volume inspect \
  --format '{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}' \
  reminder_bot_postgres_data)"
test "${volume_metadata}" = "reminder_bot_postgres_data|reminder_bot|postgres_data"
pg_version="$(sudo -u reminder-deploy docker run --rm \
  --mount type=volume,source=reminder_bot_postgres_data,target=/var/lib/postgresql/data,readonly \
  postgres:17 sh -c 'cat /var/lib/postgresql/data/PG_VERSION')"
test "${pg_version}" = 17
```

If any check fails, stop with `HUMAN_REQUIRED`. Do not run `docker volume
create`, do not rename/copy PostgreSQL data files, and do not use `down` with
volume removal. Validate Compose syntax only:

```bash
sudo -u reminder-deploy env \
  REMINDER_BOT_IMAGE=reminder-bot:local \
  APP_PUBLISH_HOST=127.0.0.1 \
  docker compose -p reminder_bot config --quiet
```

This procedure deliberately does not run `docker compose up`, migrations, or
the deployment script. It also never inspects, restarts, stops, prunes, or
reconfigures `mtproxy`.

### 5. Configure owner-controlled GitHub values, but leave deployment off

Variables:

- `DEPLOY_ENABLED` (leave unset or `false`);
- `PROD_SSH_HOST`;
- `PROD_SSH_PORT`;
- `PROD_SSH_USER=reminder-deploy`;
- `PROD_APP_DIR=/opt/reminder-bot`.

Secrets:

- `PROD_SSH_PRIVATE_KEY`;
- `PROD_SSH_KNOWN_HOSTS`.

The known-host entry must be constructed from the owner-verified VPS host key
fingerprint, not from runtime `ssh-keyscan`. Do not create or change these
GitHub values as part of this repository task.

## Restoring production `.env` safely

If `/opt/reminder-bot/.env` is restored from a backup or rollback, the owner
must verify all of the following before enabling a deploy:

- it is a regular file and not a symlink;
- owner and group are both `reminder-deploy`;
- mode is exactly `0600`;
- `reminder-deploy` can open it for reading.

The repair is an owner-controlled operation, outside the deploy script:

```bash
sudo chown reminder-deploy:reminder-deploy /opt/reminder-bot/.env
sudo chmod 0600 /opt/reminder-bot/.env
```

Restoring mode `0600` alone is insufficient if ownership has become
`root:root`. Do not print the file contents, automate these root actions in the
deploy, touch the legacy `/root/reminder_bot`, or restore the canonical `.env`
while another deployment is active or waiting for its lock.

## Deployment sequence and safety gates

For an enabled, correctly bootstrapped deployment, the workflow and remote
script perform this bounded sequence:

1. validate the exact 40-character SHA and the successful `master` CI run;
2. run the fail-fast production `.env` invariant/readability checks and
   `BOT_MODE=polling` validation before any deployment lock is acquired;
3. acquire the Reminder Bot-specific server lock
   `/opt/reminder-bot/locks/deploy.lock` with a bounded `flock` wait, then
   repeat the `.env` validation and pin its identity/content fingerprint;
4. check out and load `reminder-bot:<FULL_SHA>`, verifying OCI labels;
5. validate project `reminder_bot`, the exact named volume, labels, PG17, and
   the polling invariant before live service changes;
6. re-fetch `origin/master` and require the requested SHA;
7. start/check PostgreSQL with `docker compose -p reminder_bot up -d --no-build db`;
8. create a private custom-format `pg_dump` under
   `/opt/reminder-bot/backups/<UTC>_pre-deploy_<FULL_SHA>.dump`, require success
   and a non-empty file, then retain only the latest ten deploy-created dumps;
9. re-fetch `origin/master`, then run the `migrate` service with
   `--no-build`; backup failure or migration failure stops before bot/worker
   replacement;
10. re-fetch `origin/master` before application rollout, then replace only the
   single `bot` and `worker` services with
   `docker compose -p reminder_bot up -d --no-build --no-deps --force-recreate bot worker`;
11. require bot existence, running state, exact image, configured healthcheck
    health, and local `/healthz` plus `/readyz` HTTP 200 responses;
12. require the worker to have the exact image and running state, record its
    restart count, observe it for ten seconds, and require the same container
    and restart count at the end;
13. re-fetch `origin/master`, atomically write the full SHA to
    `/opt/reminder-bot/state/deployed-sha`, and emit
    `Deployment verdict: ACTIVE <FULL_SHA>`. GitHub Actions captures the remote
    output and fails closed unless that exact line matches the requested SHA.

The lock path is Reminder Bot-specific and cannot affect `mtproxy`. A second
deployment waits at most 30 seconds and then fails without Docker service or
database mutation. GitHub Actions additionally serializes the production
workflow with `cancel-in-progress: false`.

## Stale and failure recovery

- A stale SHA before database mutation fails closed before starting production
  services.
- If `master` advances while PostgreSQL starts or while the backup is made,
  the deploy stops before migration.
- If `master` advances after migration commits, the deploy fails visibly before
  bot/worker replacement. No automatic database downgrade is attempted; let a
  newer successful `master` deployment follow.
- Migration failure leaves the existing bot/worker containers in place.
- Readiness or worker-observation failure after rollout is a visible failed
  deployment requiring diagnosis. Do not delete or replace the PostgreSQL
  volume to recover.
- Application rollback is a normal revert merged through protected `master`,
  followed by green CI and a new exact-SHA deployment. Database recovery is a
  separate owner-approved operation.

The script never builds an application image on the VPS and never uses global
Docker prune commands, volume deletion, destructive Compose volume options,
raw PostgreSQL file copying, or commands against an unrelated Compose project.

## Secrets and logs

The workflow never uploads or prints `.env`, database passwords, private keys,
or known-host contents. The private SSH material exists only on the ephemeral
runner and is removed in an `always()` cleanup step. Backup output contains
database data but remains on the VPS under a private directory and is never
sent to CI logs.
