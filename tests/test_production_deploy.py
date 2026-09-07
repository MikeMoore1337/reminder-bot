from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_production.sh"


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
        "Reject stale master target",
        "Validate successful master CI",
        "event=push&status=success",
        "StrictHostKeyChecking=yes",
        "PROD_SSH_PRIVATE_KEY",
        "PROD_SSH_KNOWN_HOSTS",
        "PROD_APP_DIR",
        "< scripts/deploy_production.sh",
    )
    for fragment in required_fragments:
        assert fragment in workflow

    assert "ssh-keyscan" not in workflow
    assert "pull_request:" not in workflow
    assert "DEPLOY_ENABLED == 'false'" not in workflow


def test_remote_deploy_script_excludes_destructive_shortcuts() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    required_fragments = (
        "git status --porcelain --untracked-files=all",
        "git fetch --no-tags --prune origin master",
        'origin_master_sha="$(git rev-parse origin/master)"',
        "git checkout --detach --quiet",
        "docker compose config --quiet",
        "docker compose build",
        "docker compose up -d db",
        "docker compose --profile tools run --rm migrate",
        "docker compose up -d bot worker",
        "/readyz",
        "docker compose ps -q worker",
        "DEPLOYED_SHA=",
    )
    for fragment in required_fragments:
        assert fragment in script

    forbidden_fragments = (
        "docker compose down",
        "--volumes",
        "git clean",
        "git reset --hard",
        "alembic downgrade",
        "ssh-keyscan",
    )
    for fragment in forbidden_fragments:
        assert fragment not in script


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="behavioral deployment contract requires bash and git",
)
def test_remote_deploy_script_rejects_dirty_and_stale_state_and_runs_exact_sha(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    app = tmp_path / "app"
    fake_bin = tmp_path / "fake-bin"
    docker_calls = tmp_path / "docker-calls.log"

    _run(["git", "init", "--bare", str(origin)])
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/master"], cwd=origin)

    seed.mkdir()
    _run(["git", "init"], cwd=seed)
    _git(seed, "checkout", "-b", "master")
    _git(seed, "config", "user.name", "Deploy Contract Test")
    _git(seed, "config", "user.email", "deploy-test@example.invalid")
    (seed / ".gitignore").write_text(".env\n", encoding="utf-8")
    (seed / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (seed / "tracked.txt").write_text("version-1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "master")

    _run(["git", "clone", str(origin), str(app)])
    (app / ".env").write_text("BOT_TOKEN=test-only\n", encoding="utf-8")
    exact_sha = _git(app, "rev-parse", "HEAD")
    assert len(exact_sha) == 40

    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' \"$*\" >> \"${DOCKER_CALLS}\"
if [[ \"${1:-}\" == \"compose\" && \"${2:-}\" == \"ps\" && \"${3:-}\" == \"-q\" && \"${4:-}\" == \"worker\" ]]; then
  printf 'fake-worker-id\\n'
fi
if [[ \"${1:-}\" == \"inspect\" ]]; then
  printf 'running\\n'
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["DOCKER_CALLS"] = str(docker_calls)

    success = _run(
        ["bash", str(DEPLOY_SCRIPT), str(app), exact_sha],
        env=env,
        check=False,
    )
    assert success.returncode == 0, success.stderr
    assert f"DEPLOYED_SHA={exact_sha}" in success.stdout
    calls = docker_calls.read_text(encoding="utf-8")
    expected_order = (
        "compose config --quiet",
        "compose build",
        "compose up -d db",
        "compose --profile tools run --rm migrate",
        "compose up -d bot worker",
        "compose exec -T bot python -c",
        "compose ps -q worker",
        "inspect --format {{.State.Status}} fake-worker-id",
    )
    cursor = -1
    for command in expected_order:
        next_cursor = calls.find(command, cursor + 1)
        assert next_cursor > cursor, calls
        cursor = next_cursor

    docker_calls.write_text("", encoding="utf-8")
    stale = _run(
        ["bash", str(DEPLOY_SCRIPT), str(app), "0" * 40],
        env=env,
        check=False,
    )
    assert stale.returncode != 0
    assert "stale deploy target" in stale.stderr
    assert docker_calls.read_text(encoding="utf-8") == ""

    (app / "tracked.txt").write_text("local-change\n", encoding="utf-8")
    dirty = _run(
        ["bash", str(DEPLOY_SCRIPT), str(app), exact_sha],
        env=env,
        check=False,
    )
    assert dirty.returncode != 0
    assert "local worktree changes" in dirty.stderr
    assert docker_calls.read_text(encoding="utf-8") == ""
