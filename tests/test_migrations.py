from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def _script_directory() -> ScriptDirectory:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    return ScriptDirectory.from_config(config)


def test_alembic_has_exactly_one_current_head() -> None:
    script = _script_directory()

    assert script.get_heads() == ["20260907_0004"]


def test_alembic_head_has_the_expected_linear_history() -> None:
    script = _script_directory()
    expected = {
        "20260907_0004": "20260907_0003",
        "20260907_0003": "20260331_0002",
        "20260331_0002": "20260330_0001",
        "20260330_0001": None,
    }

    for revision, down_revision in expected.items():
        assert script.get_revision(revision).down_revision == down_revision
