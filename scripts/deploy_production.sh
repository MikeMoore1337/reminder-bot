#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

fail() {
  local message="$*"
  printf 'DEPLOY ERROR: %s\n' "${message}" >&2
  if [[ -n "${expected_sha:-}" ]]; then
    printf 'Deployment verdict: FAILED %s\n' "${expected_sha}" >&2
  else
    printf 'Deployment verdict: FAILED\n' >&2
  fi
  exit 1
}

[[ $# -eq 2 ]] || fail "usage: deploy_production.sh <repo-dir> <expected-sha>"

repo_dir="$1"
expected_sha="$2"
expected_image="reminder-bot:${expected_sha}"
expected_image_source="https://github.com/MikeMoore1337/reminder-bot"
compose_project="reminder_bot"
expected_volume="reminder_bot_postgres_data"
expected_volume_name="postgres_data"
lock_wait_seconds="${REMINDER_BOT_LOCK_WAIT_SECONDS:-30}"
state_dir="${REMINDER_BOT_STATE_DIR:-${repo_dir}/state}"
backup_dir="${REMINDER_BOT_BACKUP_DIR:-${repo_dir}/backups}"
lock_path="${REMINDER_BOT_DEPLOY_LOCK_PATH:-${repo_dir}/locks/deploy.lock}"
marker_path="${state_dir}/deployed-sha"
backup_tmp=""
marker_tmp=""

[[ "${repo_dir}" == /* ]] || fail "repository path must be absolute"
[[ "${repo_dir}" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "repository path contains unsupported characters"
[[ "${expected_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "expected SHA must be a full lowercase commit SHA"
[[ "${REMINDER_BOT_IMAGE:-}" == "${expected_image}" ]] || fail "REMINDER_BOT_IMAGE does not match expected SHA"
[[ "${lock_wait_seconds}" =~ ^[0-9]+$ ]] || fail "lock wait must be an integer"
(( lock_wait_seconds >= 1 && lock_wait_seconds <= 300 )) || fail "lock wait is out of bounds"

for tool in awk chmod date docker find flock git mkdir mktemp mv rm sleep stat; do
  command -v "${tool}" >/dev/null 2>&1 || fail "${tool} is required"
done

[[ -d "${repo_dir}/.git" ]] || fail "repository is not initialized at ${repo_dir}"
[[ -f "${repo_dir}/.env" ]] || fail "production .env is missing"
env_mode="$(stat -c '%a' -- "${repo_dir}/.env" 2>/dev/null)" || fail "cannot inspect production .env permissions"
[[ "${env_mode}" == "600" ]] || fail "production .env must have mode 0600"

# Polling production must have one bot owner. The HTTP probe remains available
# on loopback, while the production host publish binding is forced below.
if ! awk -F= '
  function trim(value) {
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
    return value
  }
  $1 == "BOT_MODE" { mode = trim($2) }
  END { exit(mode == "polling" ? 0 : 1) }
' "${repo_dir}/.env"; then
  fail "production BOT_MODE must be polling"
fi

cd "${repo_dir}"

# Ignored .env/runtime state is allowed. Tracked changes and ordinary
# untracked files would alter the checked-out source or build inputs, so fail
# before creating the deployment lock or touching Docker.
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  fail "production repository has local worktree changes"
fi

export COMPOSE_PROJECT_NAME="${compose_project}"
export REMINDER_BOT_IMAGE="${expected_image}"
export APP_PUBLISH_HOST="127.0.0.1"

lock_dir="${lock_path%/*}"
mkdir -p -- "${lock_dir}"
chmod 700 -- "${lock_dir}"
exec {lock_fd}>"${lock_path}" || fail "cannot open deployment lock"
if ! flock -w "${lock_wait_seconds}" "${lock_fd}"; then
  fail "another Reminder Bot deployment holds the server lock"
fi

cleanup() {
  if [[ -n "${backup_tmp}" ]]; then
    rm -f -- "${backup_tmp}"
  fi
  if [[ -n "${marker_tmp}" ]]; then
    rm -f -- "${marker_tmp}"
  fi
}
trap cleanup EXIT

compose=(docker compose -p "${compose_project}")
compose_tools=(docker compose -p "${compose_project}" --profile tools)

assert_current_master() {
  git fetch --no-tags --prune origin master
  local origin_master_sha
  origin_master_sha="$(git rev-parse origin/master)"
  [[ "${origin_master_sha}" == "${expected_sha}" ]] || fail \
    "stale deploy target: origin/master=${origin_master_sha}, expected=${expected_sha}"
}

validate_loaded_image() {
  local image_revision image_source
  image_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${expected_image}" 2>/dev/null)" \
    || fail "exact deployment image is not loaded"
  [[ "${image_revision}" == "${expected_sha}" ]] || fail "deployment image revision label mismatch"
  image_source="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source"}}' "${expected_image}" 2>/dev/null)" \
    || fail "deployment image source label is missing"
  [[ "${image_source}" == "${expected_image_source}" ]] || fail "deployment image source label mismatch"
}

validate_existing_volume() {
  local metadata pg_version
  metadata="$(docker volume inspect --format '{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}' "${expected_volume}" 2>/dev/null)" \
    || fail "expected production PostgreSQL volume is missing"
  [[ "${metadata}" == "${expected_volume}|${compose_project}|${expected_volume_name}" ]] || fail \
    "production PostgreSQL volume identity or labels are unexpected"

  pg_version="$(docker run --rm \
    --mount "type=volume,source=${expected_volume},target=/var/lib/postgresql/data,readonly" \
    postgres:17 sh -c 'cat /var/lib/postgresql/data/PG_VERSION' 2>/dev/null)" \
    || fail "cannot read PG_VERSION from the production PostgreSQL volume"
  [[ "${pg_version}" == "17" ]] || fail "production PostgreSQL volume is not PostgreSQL 17"
}

prune_backups() {
  local backup_name index
  local -a backup_names valid_names
  backup_names=()
  valid_names=()
  mapfile -t backup_names < <(
    find "${backup_dir}" -maxdepth 1 -type f -name '*_pre-deploy_*.dump' -printf '%f\n' | sort -r
  )
  for backup_name in "${backup_names[@]}"; do
    if [[ "${backup_name}" =~ ^[0-9]{8}T[0-9]{6}Z_pre-deploy_[0-9a-f]{40}\.dump$ ]]; then
      valid_names+=("${backup_name}")
    fi
  done
  for ((index = 10; index < ${#valid_names[@]}; index++)); do
    rm -f -- "${backup_dir}/${valid_names[index]}"
  done
}

wait_for_db_healthy() {
  local attempt db_id db_health
  for ((attempt = 1; attempt <= 30; attempt++)); do
    db_id="$("${compose[@]}" ps -q db 2>/dev/null || true)"
    if [[ -n "${db_id}" ]]; then
      db_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${db_id}" 2>/dev/null || true)"
      if [[ "${db_health}" == "healthy" ]]; then
        return 0
      fi
      [[ "${db_health}" != "unhealthy" ]] || fail "PostgreSQL container is unhealthy"
    fi
    sleep 2
  done
  fail "PostgreSQL container did not become healthy within 60 seconds"
}

create_backup() {
  local timestamp backup_path
  mkdir -p -- "${backup_dir}"
  chmod 700 -- "${backup_dir}"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="${backup_dir}/${timestamp}_pre-deploy_${expected_sha}.dump"
  [[ ! -e "${backup_path}" ]] || fail "backup path already exists"
  backup_tmp="$(mktemp -- "${backup_dir}/.pre-deploy-${expected_sha}.XXXXXX")" \
    || fail "cannot create private backup temporary file"

  if "${compose[@]}" exec -T db sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --format=custom --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
    >"${backup_tmp}"; then
    :
  else
    fail "PostgreSQL backup failed"
  fi
  [[ -s "${backup_tmp}" ]] || fail "PostgreSQL backup is empty"
  chmod 600 -- "${backup_tmp}"
  mv -f -- "${backup_tmp}" "${backup_path}"
  backup_tmp=""
  prune_backups
}

container_id() {
  local service="$1"
  local id
  id="$("${compose[@]}" ps -q "${service}" 2>/dev/null || true)"
  [[ -n "${id}" ]] || fail "${service} container is missing"
  printf '%s\n' "${id}"
}

require_exact_running_container() {
  local service="$1"
  local id state image
  id="$(container_id "${service}")"
  state="$(docker inspect --format '{{.State.Status}}' "${id}")"
  [[ "${state}" == "running" ]] || fail "${service} container is not running"
  image="$(docker inspect --format '{{.Config.Image}}' "${id}")"
  [[ "${image}" == "${expected_image}" ]] || fail "${service} is not using the exact deployment image"
  printf '%s\n' "${id}"
}

verify_bot() {
  local bot_id bot_state bot_health bot_image
  bot_id="$("${compose[@]}" ps -q bot 2>/dev/null || true)"
  [[ -n "${bot_id}" ]] || return 1
  bot_state="$(docker inspect --format '{{.State.Status}}' "${bot_id}" 2>/dev/null || true)"
  [[ "${bot_state}" == "running" ]] || return 1
  bot_image="$(docker inspect --format '{{.Config.Image}}' "${bot_id}" 2>/dev/null || true)"
  [[ "${bot_image}" == "${expected_image}" ]] || return 1
  bot_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${bot_id}" 2>/dev/null || true)"
  [[ "${bot_health}" == "none" || "${bot_health}" == "healthy" ]] || return 1
  "${compose[@]}" exec -T bot python -c \
    'import urllib.request
for path in ("/healthz", "/readyz"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)' \
    >/dev/null 2>&1
}

assert_current_master
git cat-file -e "${expected_sha}^{commit}" 2>/dev/null || fail "expected commit is unavailable"
git checkout --detach --quiet "${expected_sha}"
[[ "$(git rev-parse HEAD)" == "${expected_sha}" ]] || fail "checkout did not land on expected SHA"

validate_loaded_image
validate_existing_volume
"${compose[@]}" config --quiet

configured_app_images="$("${compose_tools[@]}" config --images | awk -v expected="${expected_image}" '$0 == expected { count++ } END { print count + 0 }')"
[[ "${configured_app_images}" == "3" ]] || fail "migrate, bot, and worker do not share the exact image"

assert_current_master
"${compose[@]}" up -d --no-build db
wait_for_db_healthy

# The database may now be running, but no backup or migration is allowed for a
# target that became stale while PostgreSQL was starting.
assert_current_master
create_backup
assert_current_master

# Use Compose up for the one-shot service because this Compose version exposes
# --no-build on up, while `run` has no equivalent flag. The selected service is
# independent because PostgreSQL was started and checked above.
migration_status=0
if "${compose_tools[@]}" up --no-build --no-deps --abort-on-container-exit --exit-code-from migrate migrate; then
  migration_status=0
else
  migration_status=$?
fi
"${compose_tools[@]}" rm --force migrate >/dev/null 2>&1 || true
(( migration_status == 0 )) || fail "database migration failed"

# Once migration has committed, a stale target is still a visible failure; no
# automatic database downgrade is attempted.
assert_current_master
"${compose[@]}" up -d --no-build --force-recreate bot worker

bot_ready=0
for ((attempt = 1; attempt <= 30; attempt++)); do
  if verify_bot; then
    bot_ready=1
    break
  fi
  sleep 2
done
[[ "${bot_ready}" -eq 1 ]] || fail "bot health/readiness did not become healthy within 60 seconds"

worker_id="$(require_exact_running_container worker)"
worker_restart_before="$(docker inspect --format '{{.RestartCount}}' "${worker_id}")"
[[ "${worker_restart_before}" =~ ^[0-9]+$ ]] || fail "worker restart count is unavailable"
sleep 10
worker_id_after="$(require_exact_running_container worker)"
[[ "${worker_id_after}" == "${worker_id}" ]] || fail "worker container changed during observation"
worker_restart_after="$(docker inspect --format '{{.RestartCount}}' "${worker_id_after}")"
[[ "${worker_restart_after}" == "${worker_restart_before}" ]] || fail "worker restarted during observation"

assert_current_master
mkdir -p -- "${state_dir}"
chmod 700 -- "${state_dir}"
marker_tmp="$(mktemp -- "${state_dir}/.deployed-sha.XXXXXX")" || fail "cannot create deployment marker"
printf '%s\n' "${expected_sha}" >"${marker_tmp}"
chmod 600 -- "${marker_tmp}"
mv -f -- "${marker_tmp}" "${marker_path}"
marker_tmp=""

printf 'Worker observation: running restart_count=%s duration=10s\n' "${worker_restart_after}"
printf 'Deployment verdict: ACTIVE %s\n' "${expected_sha}"
