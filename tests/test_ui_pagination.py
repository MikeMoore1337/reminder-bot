import os
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.handlers.ui import _list_page_bounds, _parse_list_page


def test_list_pages_cover_every_reminder_deterministically() -> None:
    pages = [_list_page_bounds(700, page) for page in range(1, 36)]

    assert pages[0] == (1, 35, 0, 20)
    assert pages[-1] == (35, 35, 680, 700)
    assert [(start, end) for _, _, start, end in pages] == [
        (index, min(index + 20, 700)) for index in range(0, 700, 20)
    ]
    assert _list_page_bounds(700, 999) == (35, 35, 680, 700)


def test_list_page_command_argument_is_bounded() -> None:
    assert _parse_list_page(SimpleNamespace(args=None)) == 1
    assert _parse_list_page(SimpleNamespace(args="2")) == 2
    assert _parse_list_page(SimpleNamespace(args="700 extra")) == 700
    assert _parse_list_page(SimpleNamespace(args="0")) == 1
    assert _parse_list_page(SimpleNamespace(args="not-a-page")) == 1
