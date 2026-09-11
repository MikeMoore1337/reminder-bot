from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_production.sh"
SSH_WRAPPER = ROOT / "deploy" / "scripts" / "reminder_bot_ssh_wrapper.sh"
DOCKERIGNORE = ROOT / ".dockerignore"

UNIX_DEPLOY_TOOLS = all(shutil.which(tool) for tool in ("bash", "flock", "git"))


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd=cwd).stdout.strip()


def _service_block(compose: str, service: str) -> str:
    lines = compose.splitlines()
    start = lines.index(f"  {service}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  [a-z][a-z0-9_-]*:$", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _workflow_step_script(step_name: str) -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines(keepends=True)
    marker = f"      - name: {step_name}\n"
    start = lines.index(marker)
    run_start = lines.index("        run: |\n", start) + 1
    body = []
    for line in lines[run_start:]:
        if line.startswith("      - name:"):
            break
        if line.startswith("          "):
            body.append(line[10:])
        elif line.strip():
            raise AssertionError(f"unexpected workflow indentation in {step_name}: {line!r}")
        else:
            body.append(line)
    return "".join(body)


def test_production_workflow_is_fail_closed_and_disabled_by_default() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    required_fragments = (
        'workflows: ["CI"]',
        "branches: [master]",
        "workflow_dispatch:",
        "vars.DEPLOY_ENABLED == 'true'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.head_branch == 'master'",
        "github.ref == 'refs/heads/master'",
        "cancel-in-progress: false",
        "docker build",
        'image="reminder-bot:${DEPLOY_SHA}"',
        '--tag "${image}"',
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "docker save",
        "gzip -1",
        "docker load",
        "REMINDER_BOT_IMAGE",
        "Reject stale master target",
        "Validate successful master CI",
        "StrictHostKeyChecking=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "RequestTTY=no",
        "PROD_SSH_PRIVATE_KEY",
        "PROD_SSH_KNOWN_HOSTS",
        "PROD_APP_DIR",
        "< scripts/deploy_production.sh",
        'remote_log="$(mktemp',
        "trap 'rm -f -- \"${remote_log}\"' EXIT",
        "2>&1 |",
        'tee "${remote_log}"',
        'pipeline_statuses=("${PIPESTATUS[@]}")',
        'expected_verdict="Deployment verdict: ACTIVE ${DEPLOY_SHA}"',
        'grep -Fqx -- "${expected_verdict}" "${remote_log}"',
        "Remote production deploy did not emit exact ACTIVE verdict",
    )
    for fragment in required_fragments:
        assert fragment in workflow

    assert workflow.count("docker build") == 1
    assert "docker compose build" not in workflow
    assert "ssh-keyscan" not in workflow
    assert "pull_request:" not in workflow
    assert "DEPLOY_ENABLED == 'false'" not in workflow
    assert "docker system prune" not in workflow
    assert "docker volume prune" not in workflow
    assert "docker network prune" not in workflow
    assert "down -v" not in workflow
    assert "mtproxy" not in workflow


def test_compose_uses_one_parameterized_application_image_and_preserves_volume() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    expected_image = "image: ${REMINDER_BOT_IMAGE:-reminder-bot:local}"

    assert compose.count(expected_image) == 3
    assert compose.count("build: .") == 3
    for service in ("migrate", "bot", "worker"):
        assert expected_image in _service_block(compose, service)

    assert "name: reminder_bot_postgres_data" in compose
    assert '"${APP_PUBLISH_HOST:-0.0.0.0}:8080:8080"' in compose


def test_docker_context_excludes_gitignored_runtime_artifacts() -> None:
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")

    for pattern in (
        ".env",
        ".idea/",
        ".ruff_cache/",
        "*.sqlite",
        "*.sqlite3",
        "*.db",
        "*.db-journal",
        "test-results/",
        "test-artifacts/",
        "backups/",
        "locks/",
        "state/",
        "audio/",
        "media/",
        "stt-models/",
        "*.wav",
        "*.mp3",
        "*.ogg",
        "*.flac",
    ):
        assert pattern in dockerignore


def test_remote_deploy_script_contains_the_production_safety_contract() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    required_fragments = (
        'expected_image="reminder-bot:${expected_sha}"',
        'REMINDER_BOT_IMAGE:-}" == "${expected_image}',
        'REMINDER_BOT_IMAGE="${expected_image}"',
        "flock -w",
        'compose_project="reminder_bot"',
        'expected_volume="reminder_bot_postgres_data"',
        "com.docker.compose.project",
        "com.docker.compose.volume",
        "PG_VERSION",
        "postgres:17",
        "--format=custom",
        "pg_dump",
        "exec --interactive=false -T db",
        "exec --interactive=false -T bot",
        "/dev/null",
        "pre-deploy_",
        'backup_tmp="$(mktemp',
        'mv -f -- "${backup_tmp}"',
        "deployed-sha",
        "Deployment verdict: ACTIVE",
        "BOT_MODE",
        'APP_PUBLISH_HOST="127.0.0.1"',
        "/healthz",
        "/readyz",
        "RestartCount",
        "duration=10s",
        "--no-build",
        "--no-deps",
        "--force-recreate",
        "--abort-on-container-exit",
        "--exit-code-from migrate",
        "app.services.voice_runtime",
        "--interactive=false",
        "< /dev/null",
        "validate_production_env",
        "stat -c '%d %i %u %g %a'",
        "sha256sum",
        "production_env_fingerprint",
        "production .env changed during deployment",
        "id -u",
        "id -g",
        "owner/group must match deployment account",
        "symlinks are not allowed",
        "exec {env_read_fd}<",
        "not readable by deployment account",
        "could not be read while validating BOT_MODE",
    )
    for fragment in required_fragments:
        assert fragment in script

    assert "docker compose build" not in script
    assert "docker system prune" not in script
    assert "docker volume prune" not in script
    assert "docker network prune" not in script
    assert "docker compose down" not in script
    assert "--volumes" not in script
    assert "git clean" not in script
    assert "git reset --hard" not in script
    assert "alembic downgrade" not in script
    assert "ssh-keyscan" not in script
    assert "mtproxy" not in script

    backup_call = script.index("create_backup\nassert_current_master")
    migration_call = script.index("migration_status=0")
    assert backup_call < migration_call
    env_preflight_call = script.index("\nvalidate_production_env\n")
    env_post_lock_call = script.rindex("\nvalidate_production_env\n")
    bot_mode_call = script.index("\nvalidate_bot_mode\n")
    bot_mode_post_lock_call = script.rindex("\nvalidate_bot_mode\n")
    lock_call = script.index("exec {lock_fd}")
    assert (
        env_preflight_call
        < bot_mode_call
        < lock_call
        < env_post_lock_call
        < bot_mode_post_lock_call
    )
    assert script.count("\nvalidate_bot_mode\n") == 2
    assert script.count("assert_production_env_unchanged") >= 15

    compose_invocations = re.findall(r"(?m)^compose(?:_tools)?=\(docker compose.*$", script)
    assert compose_invocations
    assert all('-p "${compose_project}"' in invocation for invocation in compose_invocations)
    assert script.count("exec --interactive=false -T") == 2
    assert script.count("</dev/null") == 2
    assert "exec -T" not in script


def test_bootstrap_documentation_covers_legacy_env_and_restricted_ssh() -> None:
    docs = (ROOT / "docs" / "production-deploy.md").read_text(encoding="utf-8")

    for fragment in (
        "/root/reminder_bot",
        "0600",
        "reminder-deploy",
        "/bin/bash",
        "functional `/bin/bash` login shell",
        "HUMAN_REQUIRED",
        "reminder_bot_postgres_data",
        "com.docker.compose.project",
        "PG_VERSION",
        "restrict",
        "AllowAgentForwarding no",
        "AllowTcpForwarding no",
        "X11Forwarding no",
        "PermitTTY no",
        "PasswordAuthentication no",
        "reminder_bot_ssh_wrapper.sh",
        "does not run `docker compose up`",
        "mtproxy",
        "DEPLOY_ENABLED",
    ):
        assert fragment in docs

    wrapper = SSH_WRAPPER.read_text(encoding="utf-8")
    for fragment in (
        "SSH_ORIGINAL_COMMAND",
        '"docker load"',
        "/etc/reminder-bot/deploy-app-dir",
        "image_sha",
        "deploy_sha",
        "exec /usr/bin/env",
        "SSH command is not allowed",
    ):
        assert fragment in wrapper


def test_bootstrap_documentation_is_clone_first_and_shell_compatible() -> None:
    docs = (ROOT / "docs" / "production-deploy.md").read_text(encoding="utf-8")
    bootstrap_section = docs.split(
        "### 1. Create the dedicated account, clone, and then runtime directories", 1
    )[1].split("### 2. Preserve and copy the production environment securely", 1)[0]
    code_blocks = re.findall(r"```bash\n(.*?)```", bootstrap_section, flags=re.DOTALL)
    assert len(code_blocks) == 1
    bootstrap = code_blocks[0]

    clone_index = bootstrap.index("git clone")
    runtime_index = bootstrap.index('"${app_dir}/backups"')
    empty_check_index = bootstrap.index("mindepth 1")

    assert "--shell /bin/bash" in bootstrap
    assert "/usr/sbin/nologin" not in bootstrap
    assert empty_check_index < clone_index < runtime_index
    assert "HUMAN_REQUIRED" in bootstrap
    assert 'sudo test -L "${app_dir}"' in bootstrap
    assert not any(
        f'"${{app_dir}}/{runtime_dir}"' in bootstrap[:clone_index]
        for runtime_dir in ("backups", "locks", "state")
    )
    assert "sudo -u reminder-deploy git clone" in bootstrap
    assert '"${app_dir}"' in bootstrap

    section2_start = docs.index("### 2. Preserve and copy the production environment securely")
    section3_start = docs.index("### 3. Install the restricted SSH command boundary")
    section4_start = docs.index(
        "### 4. Verify tools and the existing PostgreSQL volume without starting services"
    )
    env_section = docs[section2_start:section3_start]
    ssh_section = docs[section3_start:section4_start]
    volume_section = docs[section4_start:]

    assert "/root/reminder_bot/.env" in env_section
    assert "authorized-keys" in ssh_section
    assert "reminder_bot_postgres_data" in volume_section
    assert "PG_VERSION" in volume_section
    assert docs.index("git clone") < docs.index("/opt/reminder-bot/backups")


def test_ssh_wrapper_rejects_arbitrary_original_command() -> None:
    wrapper = SSH_WRAPPER.read_text(encoding="utf-8")

    original_command = 'original_command="${SSH_ORIGINAL_COMMAND:-}"'
    assert original_command in wrapper
    assert 'if [[ "${original_command}" == "docker load" ]]' in wrapper
    assert 'fail "SSH command is not allowed"' in wrapper
    assert wrapper.index('fail "SSH command is not allowed"') > wrapper.index(original_command)


def _deployment_fixture(tmp_path: Path) -> tuple[dict[str, Path], str, dict[str, str]]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    app = tmp_path / "app"
    fake_bin = tmp_path / "fake-bin"
    docker_calls = tmp_path / "docker-calls.log"
    backup_dir = tmp_path / "backups"
    state_dir = tmp_path / "state"
    lock_path = tmp_path / "locks" / "deploy.lock"

    _run(["git", "init", "--bare", str(origin)])
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/master"], cwd=origin)

    seed.mkdir()
    _run(["git", "init"], cwd=seed)
    _git(seed, "checkout", "-b", "master")
    _git(seed, "config", "user.name", "Deploy Contract Test")
    _git(seed, "config", "user.email", "deploy-test@example.invalid")
    (seed / ".gitignore").write_text(".env\nbackups/\nlocks/\nstate/\n", encoding="utf-8")
    (seed / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (seed / "tracked.txt").write_text("version-1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "master")

    _run(["git", "clone", str(origin), str(app)])
    (app / ".env").write_text(
        "BOT_MODE=polling\nPOSTGRES_DB=reminder_bot\nPOSTGRES_USER=reminder_app\n"
        "POSTGRES_PASSWORD=test-only\n",
        encoding="utf-8",
    )
    (app / ".env").chmod(0o600)
    exact_sha = _git(app, "rev-parse", "HEAD")

    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        r"""#!/usr/bin/env bash
set -euo pipefail

calls="${DOCKER_CALLS:?}"
printf '%s\n' "$*" >> "${calls}"

advance_master() {
  printf 'advanced\n' > "${ADVANCE_SEED}/tracked.txt"
  git -C "${ADVANCE_SEED}" add tracked.txt
  git -C "${ADVANCE_SEED}" commit -m "advance master" >/dev/null
  git -C "${ADVANCE_SEED}" push origin master >/dev/null
}

if [[ "${1:-}" == "image" && "${2:-}" == "inspect" ]]; then
  if [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
    printf '%s\n' "${EXPECTED_SHA}"
  elif [[ "$*" == *"org.opencontainers.image.source"* ]]; then
    printf '%s\n' "https://github.com/MikeMoore1337/reminder-bot"
  else
    printf '%s\n' "${EXPECTED_IMAGE}"
  fi
  exit 0
fi

if [[ "${1:-}" == "volume" && "${2:-}" == "inspect" ]]; then
  if [[ "${MISSING_VOLUME:-0}" == "1" ]]; then
    exit 1
  fi
  if [[ "${MISMATCH_LABEL:-0}" == "1" ]]; then
    printf 'reminder_bot_postgres_data|wrong_project|postgres_data\n'
  else
    printf 'reminder_bot_postgres_data|reminder_bot|postgres_data\n'
  fi
  exit 0
fi

if [[ "${1:-}" == "run" ]]; then
  printf '%s\n' "${PG_VERSION_VALUE:-17}"
  exit 0
fi

if [[ "${1:-}" == "inspect" ]]; then
  if [[ "$*" == *"RestartCount"* ]]; then
    count_file="${RESTART_COUNT_FILE:?}"
    count=0
    if [[ -f "${count_file}" ]]; then
      count="$(<"${count_file}")"
    fi
    count=$((count + 1))
    printf '%s\n' "${count}" > "${count_file}"
    if [[ "${WORKER_RESTART_INCREASE:-0}" == "1" && "${count}" -ge 2 ]]; then
      printf '1\n'
    else
      printf '0\n'
    fi
  elif [[ "$*" == *"Config.Image"* ]]; then
    printf '%s\n' "${EXPECTED_IMAGE}"
  elif [[ "$*" == *"State.Health"* ]]; then
    printf 'healthy\n'
  elif [[ "$*" == *"State.Status"* ]]; then
    printf 'running\n'
  fi
  exit 0
fi

if [[ "${1:-}" == "compose" ]]; then
  if [[ "${VOICE_PREFLIGHT_FAIL:-0}" == "1" && "$*" == *"-m app.services.voice_runtime"* ]]; then
    exit 42
  fi
  if [[ "${CONSUME_UNSAFE_STDIN:-0}" == "1" \
    && ("$*" == *" exec "* || "$*" == *" run "*) \
    && "$*" != *"--interactive=false"* \
    && "$(readlink /proc/$$/fd/0)" != "/dev/null" ]]; then
    cat >/dev/null
  fi
  if [[ "$*" == *"config --images"* ]]; then
    printf 'postgres:17\n%s\n%s\n%s\n' "${EXPECTED_IMAGE}" "${EXPECTED_IMAGE}" "${EXPECTED_IMAGE}"
    exit 0
  fi
  if [[ "$*" == *"ps -q db"* ]]; then
    printf 'fake-db-id\n'
    exit 0
  fi
  if [[ "$*" == *"ps -q bot"* ]]; then
    printf 'fake-bot-id\n'
    exit 0
  fi
  if [[ "$*" == *"ps -q worker"* ]]; then
    printf 'fake-worker-id\n'
    exit 0
  fi
  if [[ "${ADVANCE_ON_DB:-0}" == "1" && "$*" == *"up -d --no-build db"* ]]; then
    advance_master
    exit 0
  fi
  if [[ "${ADVANCE_ON_BACKUP:-0}" == "1" && "$*" == *"pg_dump"* ]]; then
    advance_master
  fi
  if [[ "$*" == *"pg_dump"* ]]; then
    if [[ "${BACKUP_FAIL:-0}" == "1" ]]; then
      exit 42
    fi
    printf 'fake-postgres-custom-archive\n'
    exit 0
  fi
  if [[ "$*" == *"up --no-build"* && "$*" == *"migrate"* ]]; then
    if [[ "${ADVANCE_ON_MIGRATION:-0}" == "1" ]]; then
      advance_master
    fi
    if [[ "${MIGRATION_FAIL:-0}" == "1" ]]; then
      exit 42
    fi
    exit 0
  fi
  if [[ "$*" == *"exec --interactive=false -T bot"* || "$*" == *"exec -T bot"* ]]; then
    [[ "${READY_FAIL:-0}" != "1" ]] || exit 42
    exit 0
  fi
  exit 0
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    real_stat = shutil.which("stat")
    if real_stat is not None:
        fake_stat = fake_bin / "stat"
        fake_stat.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${STAT_OVERRIDE:-}" ]]; then
  printf '%s\\n' "${STAT_OVERRIDE}"
else
  exec "${REAL_STAT:?}" "$@"
fi
""",
            encoding="utf-8",
        )
        fake_stat.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "DOCKER_CALLS": str(docker_calls),
            "EXPECTED_SHA": exact_sha,
            "EXPECTED_IMAGE": f"reminder-bot:{exact_sha}",
            "RESTART_COUNT_FILE": str(tmp_path / "restart-count"),
            "REMINDER_BOT_IMAGE": f"reminder-bot:{exact_sha}",
            "REMINDER_BOT_BACKUP_DIR": str(backup_dir),
            "REMINDER_BOT_STATE_DIR": str(state_dir),
            "REMINDER_BOT_DEPLOY_LOCK_PATH": str(lock_path),
            "REMINDER_BOT_LOCK_WAIT_SECONDS": "1",
            "REAL_STAT": real_stat or "",
            "STAT_OVERRIDE": "",
        }
    )
    return (
        {
            "origin": origin,
            "seed": seed,
            "app": app,
            "calls": docker_calls,
            "backup": backup_dir,
            "state": state_dir,
            "lock": lock_path,
            "fake_bin": fake_bin,
        },
        exact_sha,
        env,
    )


def _run_deploy(
    fixture: tuple[dict[str, Path], str, dict[str, str]],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    paths, exact_sha, base_env = fixture
    env = base_env.copy()
    env.update(overrides)
    return _run(
        ["bash", str(DEPLOY_SCRIPT), str(paths["app"]), exact_sha],
        env=env,
        check=False,
    )


def _run_streamed_deploy(
    fixture: tuple[dict[str, Path], str, dict[str, str]],
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    paths, exact_sha, base_env = fixture
    env = base_env.copy()
    env.update(overrides)
    return subprocess.run(
        ["bash", "-s", "--", str(paths["app"]), exact_sha],
        cwd=ROOT,
        env=env,
        input=DEPLOY_SCRIPT.read_text(encoding="utf-8"),
        check=False,
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_deploy_success_uses_exact_image_project_volume_backup_and_marker(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, exact_sha, _ = fixture

    result = _run_deploy(fixture)

    assert result.returncode == 0, result.stderr
    assert f"Deployment verdict: ACTIVE {exact_sha}" in result.stdout
    assert (paths["state"] / "deployed-sha").read_text(encoding="utf-8") == f"{exact_sha}\n"
    backups = list(paths["backup"].glob("*_pre-deploy_*.dump"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "fake-postgres-custom-archive\n"
    assert backups[0].stat().st_mode & 0o777 == 0o600

    calls = paths["calls"].read_text(encoding="utf-8")
    assert "compose -p reminder_bot config --quiet" in calls
    assert (
        "compose -p reminder_bot run --rm --no-deps --interactive=false "
        "--entrypoint python bot -m app.services.voice_runtime" in calls
    )
    assert "compose -p reminder_bot up -d --no-build db" in calls
    assert "compose -p reminder_bot --profile tools up --no-build --no-deps" in calls
    rollout = "compose -p reminder_bot up -d --no-build --no-deps --force-recreate bot worker"
    assert rollout in calls
    assert "pg_dump" in calls
    assert "docker compose build" not in calls
    assert calls.index("pg_dump") < calls.index("migrate") < calls.index("bot worker")
    assert calls.index("app.services.voice_runtime") < calls.index("up -d --no-build db")
    call_lines = calls.splitlines()
    migration_index = next(index for index, line in enumerate(call_lines) if "migrate" in line)
    post_migration_compose = [
        line for line in call_lines[migration_index + 1 :] if line.startswith("compose")
    ]
    assert rollout in post_migration_compose
    assert not any(
        " db" in line and not any(read_only in line for read_only in ("ps -q db", "inspect"))
        for line in post_migration_compose
    )
    assert all(
        "compose -p reminder_bot" in line
        for line in calls.splitlines()
        if line.startswith("compose")
    )
    assert "test-only" not in result.stdout + result.stderr


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_env_preflight_rejects_wrong_mode_before_bot_mode_and_docker(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture
    (paths["app"] / ".env").chmod(0o640)

    result = _run_deploy(fixture)

    assert result.returncode != 0
    assert "production .env must have mode 0600" in result.stderr
    assert "production BOT_MODE must be polling" not in result.stderr
    assert "test-only" not in result.stdout + result.stderr
    assert not paths["calls"].exists()
    assert not paths["lock"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_env_preflight_rejects_symlink_before_reading_target(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture
    env_file = paths["app"] / ".env"
    secret_target = tmp_path / "secret.env"
    secret_target.write_text(
        "BOT_MODE=not-polling\nSECRET_VALUE=symlink-secret-value\n", encoding="utf-8"
    )
    secret_target.chmod(0o600)
    env_file.unlink()
    env_file.symlink_to(secret_target)

    result = _run_deploy(fixture)

    assert result.returncode != 0
    assert "production .env must be a regular file; symlinks are not allowed" in result.stderr
    assert "symlink-secret-value" not in result.stdout + result.stderr
    assert "production BOT_MODE must be polling" not in result.stderr
    assert not paths["calls"].exists()
    assert not paths["lock"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS or shutil.which("stat") is None,
    reason="owner regression requires Unix stat and the deployment shell",
)
def test_env_preflight_rejects_owner_group_mismatch_before_bot_mode_and_docker(
    tmp_path: Path,
) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, STAT_OVERRIDE="123 456 99999 99998 600")

    assert result.returncode != 0
    assert "production .env owner/group must match deployment account" in result.stderr
    assert "production BOT_MODE must be polling" not in result.stderr
    assert "test-only" not in result.stdout + result.stderr
    assert not paths["calls"].exists()
    assert not paths["lock"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS
    or shutil.which("stat") is None
    or not hasattr(os, "geteuid")
    or os.geteuid() == 0,
    reason="readability regression requires a non-root Unix deployment process",
)
def test_env_preflight_rejects_unreadable_file_before_bot_mode_and_docker(
    tmp_path: Path,
) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture
    (paths["app"] / ".env").chmod(0o000)

    result = _run_deploy(
        fixture,
        STAT_OVERRIDE=f"123 456 {os.geteuid()} {os.getegid()} 600",
    )

    assert result.returncode != 0
    assert "production .env is not readable by deployment account" in result.stderr
    assert "production BOT_MODE must be polling" not in result.stderr
    assert "test-only" not in result.stdout + result.stderr
    assert not paths["calls"].exists()
    assert not paths["lock"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_bot_mode_mismatch_is_reported_after_env_preflight(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture
    (paths["app"] / ".env").write_text(
        "BOT_MODE=webhook\nPOSTGRES_PASSWORD=bot-mode-secret\n", encoding="utf-8"
    )
    (paths["app"] / ".env").chmod(0o600)

    result = _run_deploy(fixture)

    assert result.returncode != 0
    assert "production BOT_MODE must be polling" in result.stderr
    assert "could not be read while validating BOT_MODE" not in result.stderr
    assert "bot-mode-secret" not in result.stdout + result.stderr
    assert not paths["calls"].exists()
    assert not paths["lock"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_voice_runtime_preflight_failure_prevents_database_start(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, VOICE_PREFLIGHT_FAIL="1")

    assert result.returncode != 0
    assert "voice runtime preflight failed" in result.stderr
    calls = paths["calls"].read_text(encoding="utf-8")
    assert "app.services.voice_runtime" in calls
    assert "up -d --no-build db" not in calls
    assert not paths["backup"].exists()
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_streamed_deploy_cannot_be_truncated_by_voice_preflight_stdin(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, exact_sha, _ = fixture

    result = _run_streamed_deploy(fixture, CONSUME_UNSAFE_STDIN="1")

    assert result.returncode == 0, result.stderr
    assert f"Deployment verdict: ACTIVE {exact_sha}" in result.stdout
    assert (paths["state"] / "deployed-sha").read_text(encoding="utf-8") == f"{exact_sha}\n"
    backups = list(paths["backup"].glob("*_pre-deploy_*.dump"))
    assert len(backups) == 1
    calls = paths["calls"].read_text(encoding="utf-8")
    preflight = (
        "compose -p reminder_bot run --rm --no-deps --interactive=false "
        "--entrypoint python bot -m app.services.voice_runtime"
    )
    assert preflight in calls
    assert calls.index("app.services.voice_runtime") < calls.index("up -d --no-build db")
    assert "pg_dump" in calls
    assert "migrate" in calls
    assert "bot worker" in calls


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_streamed_deploy_cannot_be_truncated_by_compose_exec_stdin(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, exact_sha, _ = fixture

    result = _run_streamed_deploy(fixture, CONSUME_UNSAFE_STDIN="1")

    assert result.returncode == 0, result.stderr
    assert f"Deployment verdict: ACTIVE {exact_sha}" in result.stdout
    assert (paths["state"] / "deployed-sha").read_text(encoding="utf-8") == f"{exact_sha}\n"
    calls = paths["calls"].read_text(encoding="utf-8")
    assert "migrate" in calls
    assert "bot worker" in calls
    assert "exec --interactive=false -T db" in calls
    assert "exec --interactive=false -T bot" in calls


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="workflow shell contract requires Unix bash/flock/git"
)
@pytest.mark.parametrize(
    ("remote_output", "expected_returncode"),
    (
        ("Deployment verdict: ACTIVE {sha}\n", 0),
        ("remote script exited 0 without a terminal verdict\n", 1),
        ("Deployment verdict: ACTIVE {other_sha}\n", 1),
    ),
)
def test_workflow_requires_exact_active_verdict(
    tmp_path: Path, remote_output: str, expected_returncode: int
) -> None:
    exact_sha = "a" * 40
    remote_output = remote_output.format(sha=exact_sha, other_sha="b" * 40)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat >/dev/null\n"
        "printf '%s' \"${FAKE_SSH_OUTPUT}\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    runner_home = tmp_path / "home"
    runner_temp = tmp_path / "runner-temp"
    runner_home.mkdir()
    runner_temp.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "DEPLOY_SHA": exact_sha,
            "PROD_SSH_HOST": "example.invalid",
            "PROD_SSH_PORT": "22",
            "PROD_SSH_USER": "reminder-deploy",
            "PROD_APP_DIR": "/opt/reminder-bot",
            "HOME": str(runner_home),
            "RUNNER_TEMP": str(runner_temp),
            "FAKE_SSH_OUTPUT": remote_output,
        }
    )

    result = _run(
        ["bash", "-c", _workflow_step_script("Deploy exact protected master SHA")],
        cwd=ROOT,
        env=env,
        check=False,
    )

    assert result.returncode == expected_returncode, result.stderr
    if expected_returncode:
        assert "did not emit exact ACTIVE verdict" in result.stderr
    else:
        assert remote_output in result.stdout
        assert "Verified remote deployment verdict" in result.stdout


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
@pytest.mark.parametrize(
    ("override", "message"),
    (
        ("MISSING_VOLUME", "expected production PostgreSQL volume is missing"),
        ("MISMATCH_LABEL", "identity or labels are unexpected"),
        ("PG_VERSION_VALUE", "not PostgreSQL 17"),
    ),
)
def test_volume_guards_fail_before_database_start(
    tmp_path: Path, override: str, message: str
) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture
    value = "16" if override == "PG_VERSION_VALUE" else "1"

    result = _run_deploy(fixture, **{override: value})

    assert result.returncode != 0
    assert message in result.stderr
    calls = paths["calls"].read_text(encoding="utf-8")
    assert "up -d --no-build db" not in calls
    assert not paths["backup"].exists()
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_backup_failure_prevents_migration_and_marker(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, BACKUP_FAIL="1")
    calls = paths["calls"].read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "PostgreSQL backup failed" in result.stderr
    assert "migrate" not in calls
    assert "bot worker" not in calls
    assert not (paths["state"] / "deployed-sha").exists()
    assert not list(paths["backup"].glob("*_pre-deploy_*.dump"))


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_migration_failure_prevents_bot_worker_replacement(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, MIGRATION_FAIL="1")
    calls = paths["calls"].read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "database migration failed" in result.stderr
    assert (
        "compose -p reminder_bot up -d --no-build --no-deps --force-recreate bot worker"
        not in calls
    )
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_readiness_failure_is_deployment_failure(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, READY_FAIL="1")

    assert result.returncode != 0
    assert "health/readiness" in result.stderr
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_worker_restart_count_increase_is_deployment_failure(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, WORKER_RESTART_INCREASE="1")

    assert result.returncode != 0
    assert "worker restarted during observation" in result.stderr
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_stale_sha_is_rejected_before_database_mutation(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, base_env = fixture
    (paths["seed"] / "tracked.txt").write_text("advanced before deploy\n", encoding="utf-8")
    _git(paths["seed"], "add", "tracked.txt")
    _git(paths["seed"], "commit", "-m", "advance master before deploy")
    _git(paths["seed"], "push", "origin", "master")
    result = _run(
        ["bash", str(DEPLOY_SCRIPT), str(paths["app"]), fixture[1]],
        env=base_env,
        check=False,
    )

    assert result.returncode != 0
    assert "stale deploy target" in result.stderr
    calls = paths["calls"].read_text(encoding="utf-8") if paths["calls"].exists() else ""
    assert "up -d --no-build db" not in calls
    assert "pg_dump" not in calls
    assert "migrate" not in calls
    assert not paths["state"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_stale_sha_before_migration_skips_migration_and_rollout(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, ADVANCE_ON_BACKUP="1", ADVANCE_SEED=str(paths["seed"]))
    calls = paths["calls"].read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "stale deploy target" in result.stderr
    assert "pg_dump" in calls
    assert "migrate" not in calls
    assert "bot worker" not in calls
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_stale_sha_while_postgres_starts_skips_backup_and_migration(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, ADVANCE_ON_DB="1", ADVANCE_SEED=str(paths["seed"]))
    calls = paths["calls"].read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "stale deploy target" in result.stderr
    assert "up -d --no-build db" in calls
    assert "pg_dump" not in calls
    assert "migrate" not in calls


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_stale_sha_after_migration_skips_application_rollout_without_downgrade(
    tmp_path: Path,
) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, _ = fixture

    result = _run_deploy(fixture, ADVANCE_ON_MIGRATION="1", ADVANCE_SEED=str(paths["seed"]))
    calls = paths["calls"].read_text(encoding="utf-8")

    assert result.returncode != 0
    assert "stale deploy target" in result.stderr
    assert "migrate" in calls
    assert "bot worker" not in calls
    assert "alembic downgrade" not in result.stderr
    assert not (paths["state"] / "deployed-sha").exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_server_lock_blocks_a_second_deployment_before_docker(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, base_env = fixture
    paths["lock"].parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 9>"$1"; flock -n 9; sleep 2',
            "lock-holder",
            str(paths["lock"]),
        ],
        env={
            **base_env,
            "PATH": os.pathsep.join(base_env["PATH"].split(os.pathsep)[1:]),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.1)
        result = _run_deploy(fixture)
    finally:
        holder.wait(timeout=5)

    assert result.returncode != 0
    assert "server lock" in result.stderr
    assert not paths["calls"].exists() or paths["calls"].read_text(encoding="utf-8") == ""


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS or not hasattr(os, "mkfifo"),
    reason="deployment race regression requires Unix bash/flock/git",
)
@pytest.mark.parametrize("replacement_kind", ("inode", "content"))
def test_env_replacement_while_waiting_for_lock_fails_before_docker(
    tmp_path: Path, replacement_kind: str
) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, exact_sha, base_env = fixture
    real_flock = shutil.which("flock")
    assert real_flock is not None

    flock_attempt = tmp_path / "flock-attempted"
    fake_flock = paths["fake_bin"] / "flock"
    fake_flock.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'attempted\\n' > "${FLOCK_ATTEMPT_FILE:?}"
exec "${REAL_FLOCK:?}" "$@"
""",
        encoding="utf-8",
    )
    fake_flock.chmod(0o755)

    lock_ready = tmp_path / "lock-ready"
    release_fifo = tmp_path / "release.fifo"
    os.mkfifo(release_fifo)
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            'exec 9>"$1"; "$REAL_FLOCK" -n 9; printf ready > "$2"; read -r < "$3"',
            "lock-holder",
            str(paths["lock"]),
            str(lock_ready),
            str(release_fifo),
        ],
        env={**base_env, "REAL_FLOCK": real_flock},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deploy: subprocess.Popen[str] | None = None
    holder_released = False
    try:
        deadline = time.monotonic() + 5
        while not lock_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lock_ready.exists(), "lock not held"

        deploy_env = base_env.copy()
        deploy_env.update(
            {
                "FLOCK_ATTEMPT_FILE": str(flock_attempt),
                "REAL_FLOCK": real_flock,
            }
        )
        deploy = subprocess.Popen(
            ["bash", str(DEPLOY_SCRIPT), str(paths["app"]), exact_sha],
            cwd=ROOT,
            env=deploy_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.monotonic() + 5
        while not flock_attempt.exists() and time.monotonic() < deadline:
            if deploy.poll() is not None:
                break
            time.sleep(0.01)
        assert flock_attempt.exists(), "deployment did not reach the lock wait"
        assert deploy.poll() is None

        rotated_env = "BOT_MODE=polling\nPOSTGRES_PASSWORD=rotated-secret\n"
        env_file = paths["app"] / ".env"
        if replacement_kind == "inode":
            replacement = tmp_path / "replacement.env"
            replacement.write_text(rotated_env, encoding="utf-8")
            replacement.chmod(0o600)
            os.replace(replacement, env_file)
        else:
            env_file.write_text(rotated_env, encoding="utf-8")
            env_file.chmod(0o600)

        with release_fifo.open("w", encoding="utf-8") as release:
            release.write("release\n")
        holder_released = True
        stdout, stderr = deploy.communicate(timeout=10)
    finally:
        if deploy is not None and deploy.poll() is None:
            deploy.kill()
            deploy.communicate(timeout=5)
        if not holder_released and holder.poll() is None:
            with release_fifo.open("w", encoding="utf-8") as release:
                release.write("release\n")
        holder.wait(timeout=5)

    assert deploy is not None
    assert deploy.returncode != 0
    assert "production .env changed during deployment" in stderr
    assert "rotated-secret" not in stdout + stderr
    assert not paths["calls"].exists()
    assert not paths["backup"].exists()
    assert not paths["state"].exists()


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_backup_retention_deletes_only_old_deploy_backups(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, exact_sha, _ = fixture
    paths["backup"].mkdir(parents=True)
    for index in range(12):
        timestamp = f"20260101T0000{index:02d}Z"
        (paths["backup"] / f"{timestamp}_pre-deploy_{'a' * 40}.dump").write_text(
            "old\n", encoding="utf-8"
        )
    keep_unrelated = paths["backup"] / "not-created-by-deploy.dump"
    keep_unrelated.write_text("keep\n", encoding="utf-8")

    result = _run_deploy(fixture)
    valid_backups = [
        path
        for path in paths["backup"].glob("*_pre-deploy_*.dump")
        if re.match(r"^[0-9]{8}T[0-9]{6}Z_pre-deploy_[0-9a-f]{40}\.dump$", path.name)
    ]

    assert result.returncode == 0, result.stderr
    assert len(valid_backups) == 10
    assert keep_unrelated.exists()
    assert any(exact_sha in path.name for path in valid_backups)


@pytest.mark.skipif(
    not UNIX_DEPLOY_TOOLS, reason="behavioral deployment contract requires Unix bash/flock/git"
)
def test_invalid_sha_fails_closed_before_any_docker_call(tmp_path: Path) -> None:
    fixture = _deployment_fixture(tmp_path)
    paths, _, base_env = fixture
    invalid_sha = "A" * 40
    result = _run(
        ["bash", str(DEPLOY_SCRIPT), str(paths["app"]), invalid_sha],
        env=base_env,
        check=False,
    )

    assert result.returncode != 0
    assert "full lowercase commit SHA" in result.stderr
    assert not paths["calls"].exists() or paths["calls"].read_text(encoding="utf-8") == ""
