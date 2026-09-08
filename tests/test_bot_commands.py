import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot_commands import ADMIN_COMMANDS, PUBLIC_COMMANDS


def test_deadline_command_is_in_both_canonical_command_surfaces() -> None:
    assert any(command.command == "deadline" for command in PUBLIC_COMMANDS)
    assert any(command.command == "deadline" for command in ADMIN_COMMANDS)
