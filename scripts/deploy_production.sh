#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'DEPLOY ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 2 ]] || fail "usage: deploy_production.sh <repo-dir> <expected-sha>"

repo_dir="$1"
expected_sha="$2"

[[ "${repo_dir}" == /* ]] || fail "repository path must be absolute"
[[ "${repo_dir}" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "repository path contains unsupported characters"
[[ "${expected_sha}" =~ ^[0-9a-f]{40}$ ]] || fail "expected SHA must be a full lowercase commit SHA"

command -v git >/dev/null 2>&1 || fail "git is required"
command -v docker >/dev/null 2>&1 || fail "docker is required"
[[ -d "${repo_dir}/.git" ]] || fail "repository is not initialized at ${repo_dir}"
[[ -f "${repo_dir}/.env" ]] || fail "production .env is missing"

cd "${repo_dir}"

# Ignored files such as .env are allowed, but any tracked or ordinary untracked
# worktree change can alter the Docker build context and therefore fails closed.
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  fail "production repository has local worktree changes"
fi

git fetch --no-tags --prune origin master
origin_master_sha="$(git rev-parse origin/master)"
[[ "${origin_master_sha}" == "${expected_sha}" ]] || fail \
  "stale deploy target: origin/master=${origin_master_sha}, expected=${expected_sha}"

git cat-file -e "${expected_sha}^{commit}" 2>/dev/null || fail "expected commit is unavailable"
git checkout --detach --quiet "${expected_sha}"
[[ "$(git rev-parse HEAD)" == "${expected_sha}" ]] || fail "checkout did not land on expected SHA"

# Never print the interpolated Compose configuration because it contains .env
# values. --quiet validates it without exposing secrets.
docker compose config --quiet

docker compose build

docker compose up -d db

# Migration must succeed before bot/worker rollout. No destructive downgrade,
# volume removal, or compose-wide shutdown is performed here.
docker compose --profile tools run --rm migrate

docker compose up -d bot worker

# /readyz is intentionally stronger than the container liveness check: it
# confirms the bot process can reach PostgreSQL after the migration.
ready=0
for _ in $(seq 1 30); do
  if docker compose exec -T bot python -c \
    "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2); raise SystemExit(0 if response.status == 200 else 1)" \
    >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[[ "${ready}" -eq 1 ]] || fail "bot readiness did not become healthy within 60 seconds"

worker_id="$(docker compose ps -q worker)"
[[ -n "${worker_id}" ]] || fail "worker container is missing"
worker_state="$(docker inspect --format '{{.State.Status}}' "${worker_id}")"
[[ "${worker_state}" == "running" ]] || fail "worker container is not running"

[[ "$(git rev-parse HEAD)" == "${expected_sha}" ]] || fail "repository SHA changed during deploy"
printf 'DEPLOYED_SHA=%s\n' "${expected_sha}"
