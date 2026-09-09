import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.handlers import reminders as reminders_handler
from app.handlers.guards import PRIVATE_CHAT_ONLY_TEXT
from app.services.reminder_parser import DeadlineRequest


def _message(text: str, *, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        caption=None,
        chat=SimpleNamespace(id=2002, type=chat_type),
        from_user=SimpleNamespace(id=1001),
        message_id=17,
        reply_to_message=None,
        forward_origin=None,
        entities=None,
        caption_entities=None,
        photo=None,
        document=None,
        answer=AsyncMock(),
    )


def test_bare_deadline_command_returns_usage_hint() -> None:
    message = _message("/deadline")

    asyncio.run(reminders_handler.cmd_deadline(message))

    message.answer.assert_awaited_once_with(reminders_handler.DEADLINE_FORMAT_HINT)


def test_bare_deadline_command_with_bot_mention_returns_usage_hint() -> None:
    message = _message("/deadline@bot_username")

    asyncio.run(reminders_handler.cmd_deadline(message))

    message.answer.assert_awaited_once_with(reminders_handler.DEADLINE_FORMAT_HINT)


def test_deadline_command_in_public_chat_keeps_private_chat_guard() -> None:
    message = _message("/deadline", chat_type="group")

    asyncio.run(reminders_handler.cmd_deadline(message))

    message.answer.assert_awaited_once_with(PRIVATE_CHAT_ONLY_TEXT)


@pytest.mark.parametrize(
    "command_text",
    [
        "/deadline завтра 18:00 Тест дедлайна | за час, в срок",
        "/deadline@bot_username завтра 18:00 Тест дедлайна | за час, в срок",
    ],
)
def test_full_deadline_command_keeps_draft_preview_flow(monkeypatch, command_text) -> None:
    async def scenario() -> None:
        message = _message(command_text)
        user = SimpleNamespace(id=1, chat_id=2002, timezone="Europe/Moscow")
        draft = SimpleNamespace(id=42, action_revision=3)
        sent = SimpleNamespace(message_id=99)

        async def get_user(*, telegram_user_id: int, chat_id: int):
            assert (telegram_user_id, chat_id) == (1001, 2002)
            return user

        create_draft = AsyncMock(return_value=draft)
        bind_preview = AsyncMock(return_value=True)
        parse_input = Mock(wraps=reminders_handler.parse_reminder_input)
        message.answer.return_value = sent
        monkeypatch.setattr(reminders_handler, "get_or_create_user", get_user)
        monkeypatch.setattr(reminders_handler, "parse_reminder_input", parse_input)
        monkeypatch.setattr(
            reminders_handler.reminder_service,
            "get_active_action_draft",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            reminders_handler,
            "get_active_clarification",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(reminders_handler, "cancel_active_action_drafts", AsyncMock())
        monkeypatch.setattr(reminders_handler, "cancel_clarification", AsyncMock())
        monkeypatch.setattr(reminders_handler, "cancel_voice_reminder_draft", AsyncMock())
        monkeypatch.setattr(reminders_handler, "create_deadline_draft", create_draft)
        monkeypatch.setattr(
            reminders_handler,
            "format_deadline_draft_preview",
            Mock(return_value="DEADLINE_PREVIEW"),
        )
        monkeypatch.setattr(
            reminders_handler,
            "deadline_draft_kb",
            Mock(return_value="DEADLINE_KEYBOARD"),
        )
        monkeypatch.setattr(reminders_handler, "bind_deadline_preview_message", bind_preview)

        await reminders_handler.cmd_deadline(message)

        assert (
            parse_input.call_args.args[0] == "/deadline завтра 18:00 Тест дедлайна | за час, в срок"
        )
        assert create_draft.await_count == 1
        _, parsed = create_draft.await_args.args
        assert isinstance(parsed, DeadlineRequest)
        assert parsed.text == "Тест дедлайна"
        assert parsed.point_codes == ("before_deadline", "at_deadline")
        assert create_draft.await_args.kwargs["raw_text"] == message.text
        assert create_draft.await_args.kwargs["source_message_id"] == message.message_id
        message.answer.assert_awaited_once_with(
            "DEADLINE_PREVIEW",
            reply_markup="DEADLINE_KEYBOARD",
            parse_mode="HTML",
        )
        bind_preview.assert_awaited_once_with(
            user,
            draft.id,
            revision=draft.action_revision,
            message_id=sent.message_id,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command_text",
    [
        "/deadline завтра 18:00 Новый план | за день, в срок",
        "/deadline@bot_username завтра 18:00 Новый план | за день, в срок",
    ],
)
def test_deadline_command_preserves_active_deadline_edit(monkeypatch, command_text) -> None:
    async def scenario() -> None:
        message = _message(command_text)
        user = SimpleNamespace(id=1, chat_id=2002, timezone="Europe/Moscow")
        action_draft = SimpleNamespace(
            id=7,
            action_type="deadline_edit",
            reminder_id=13,
            expected_action_revision=2,
            expected_occurrence_id=None,
            expected_occurrence_at_utc=None,
            expected_message_id=None,
            payload=None,
        )
        edited_reminder = SimpleNamespace(id=13)

        async def get_user(*, telegram_user_id: int, chat_id: int):
            assert (telegram_user_id, chat_id) == (1001, 2002)
            return user

        get_action_draft = AsyncMock(return_value=action_draft)
        edit_plan = AsyncMock(return_value=edited_reminder)
        delete_action_draft = AsyncMock()
        create_draft = AsyncMock()
        monkeypatch.setattr(reminders_handler, "get_or_create_user", get_user)
        monkeypatch.setattr(
            reminders_handler.reminder_service,
            "get_active_action_draft",
            get_action_draft,
        )
        monkeypatch.setattr(reminders_handler, "edit_deadline_plan", edit_plan)
        monkeypatch.setattr(
            reminders_handler.reminder_service,
            "delete_action_draft",
            delete_action_draft,
        )
        monkeypatch.setattr(reminders_handler, "create_deadline_draft", create_draft)
        monkeypatch.setattr(
            reminders_handler,
            "get_active_clarification",
            AsyncMock(return_value=None),
        )

        await reminders_handler.cmd_deadline(message)

        get_action_draft.assert_awaited_once_with(user)
        assert edit_plan.await_count == 1
        _, reminder_id, parsed = edit_plan.await_args.args
        assert reminder_id == action_draft.reminder_id
        assert isinstance(parsed, DeadlineRequest)
        assert parsed.text == "Новый план"
        assert parsed.point_codes == ("day_before", "at_deadline")
        delete_action_draft.assert_awaited_once_with(user, action_draft.id)
        create_draft.assert_not_awaited()
        message.answer.assert_awaited_once_with("План дедлайна изменён")

    asyncio.run(scenario())


def test_remind_command_still_uses_existing_creation_flow(monkeypatch) -> None:
    async def scenario() -> None:
        message = _message("/remind")
        create_and_answer = AsyncMock()
        monkeypatch.setattr(reminders_handler, "_create_and_answer", create_and_answer)

        await reminders_handler.cmd_remind(message)

        create_and_answer.assert_awaited_once_with(message, show_hint=True)

    asyncio.run(scenario())
