#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'DEPLOY SSH ERROR: %s\n' "$*" >&2
  exit 1
}

app_dir_file="/etc/reminder-bot/deploy-app-dir"
[[ -f "${app_dir_file}" ]] || fail "deployment app directory policy is missing"
allowed_app_dir="$(<"${app_dir_file}")"
[[ "${allowed_app_dir}" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "deployment app directory policy is invalid"

original_command="${SSH_ORIGINAL_COMMAND:-}"
if [[ "${original_command}" == "docker load" ]]; then
  exec /usr/bin/docker load
fi

if [[ "${original_command}" =~ ^REMINDER_BOT_IMAGE=reminder-bot:([0-9a-f]{40})[[:space:]]+bash[[:space:]]+-s[[:space:]]+--[[:space:]](/[A-Za-z0-9._/-]+)[[:space:]]+([0-9a-f]{40})$ ]]; then
  image_sha="${BASH_REMATCH[1]}"
  command_app_dir="${BASH_REMATCH[2]}"
  deploy_sha="${BASH_REMATCH[3]}"
  [[ "${command_app_dir}" == "${allowed_app_dir}" ]] || fail "deployment app directory is not allowed"
  [[ "${image_sha}" == "${deploy_sha}" ]] || fail "image and deployment SHA differ"
  exec /usr/bin/env "REMINDER_BOT_IMAGE=reminder-bot:${image_sha}" /bin/bash -s -- "${command_app_dir}" "${deploy_sha}"
fi

fail "SSH command is not allowed"
