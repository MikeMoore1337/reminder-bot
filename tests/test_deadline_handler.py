import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


def test_full_deadline_command_keeps_draft_preview_flow(monkeypatch) -> None:
    async def scenario() -> None:
        message = _message("/deadline завтра 18:00 Тест дедлайна | за час, в срок")
        user = SimpleNamespace(id=1, chat_id=2002, timezone="Europe/Moscow")
        draft = SimpleNamespace(id=42, action_revision=3)
        sent = SimpleNamespace(message_id=99)

        async def get_user(*, telegram_user_id: int, chat_id: int):
            assert (telegram_user_id, chat_id) == (1001, 2002)
            return user

        create_draft = AsyncMock(return_value=draft)
        bind_preview = AsyncMock(return_value=True)
        message.answer.return_value = sent
        monkeypatch.setattr(reminders_handler, "get_or_create_user", get_user)
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


def test_remind_command_still_uses_existing_creation_flow(monkeypatch) -> None:
    async def scenario() -> None:
        message = _message("/remind")
        create_and_answer = AsyncMock()
        monkeypatch.setattr(reminders_handler, "_create_and_answer", create_and_answer)

        await reminders_handler.cmd_remind(message)

        create_and_answer.assert_awaited_once_with(message, show_hint=True)

    asyncio.run(scenario())
