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
        "--force-recreate",
        "--abort-on-container-exit",
        "--exit-code-from migrate",
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

    compose_invocations = re.findall(r"(?m)^compose(?:_tools)?=\(docker compose.*$", script)
    assert compose_invocations
    assert all('-p "${compose_project}"' in invocation for invocation in compose_invocations)


def test_bootstrap_documentation_covers_legacy_env_and_restricted_ssh() -> None:
    docs = (ROOT / "docs" / "production-deploy.md").read_text(encoding="utf-8")

    for fragment in (
        "/root/reminder_bot",
        "0600",
        "reminder-deploy",
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
  if [[ "$*" == *"exec -T bot"* ]]; then
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
    assert "compose -p reminder_bot up -d --no-build db" in calls
    assert "compose -p reminder_bot --profile tools up --no-build --no-deps" in calls
    assert "compose -p reminder_bot up -d --no-build --force-recreate bot worker" in calls
    assert "pg_dump" in calls
    assert "docker compose build" not in calls
    assert calls.index("pg_dump") < calls.index("migrate") < calls.index("bot worker")
    assert all(
        "compose -p reminder_bot" in line
        for line in calls.splitlines()
        if line.startswith("compose")
    )


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
    assert "compose -p reminder_bot up -d --no-build --force-recreate bot worker" not in calls
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
