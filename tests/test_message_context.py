import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Reminder, ReminderClarification, ReminderContext, User
from app.handlers import reminders as reminders_handler
from app.services import clarification_service, message_context, reminder_service, voice_service
from app.services.message_context import (
    CONTEXT_PLACEHOLDER,
    ContextKind,
    MessageContextSnapshot,
    cleanup_expired_reminder_contexts,
    context_reminder_text,
    deserialize_context_snapshot,
    extract_message_context,
    format_context_for_delivery,
    get_context_for_delivery,
    is_contextual_message,
    parse_context_clarification_answer,
    parse_context_reminder_input,
    serialize_context_snapshot,
)
from app.workers import reminder_worker as worker


def _value(**values):
    return SimpleNamespace(**values)


def _chat(chat_id: int = 2002, *, chat_type: str = "private", username: str | None = None):
    return _value(id=chat_id, type=chat_type, username=username, title="Source chat")


def _source_message(
    *,
    message_id: int = 12,
    text: str | None = "Проверить источник",
    caption: str | None = None,
    chat=None,
    photo=None,
    document=None,
):
    return _value(
        message_id=message_id,
        date=datetime(2026, 9, 8, 7, 0, tzinfo=UTC),
        chat=chat or _chat(),
        from_user=_value(id=77, username="source_user", first_name="Source"),
        sender_chat=None,
        message_thread_id=None,
        text=text,
        caption=caption,
        entities=None,
        caption_entities=None,
        photo=photo,
        document=document,
    )


def _incoming(
    *,
    message_id: int = 90,
    text: str | None = "напомни об этом завтра в 9",
    caption: str | None = None,
    chat=None,
    reply_to_message=None,
    forward_origin=None,
    photo=None,
    document=None,
):
    return _value(
        message_id=message_id,
        date=datetime(2026, 9, 8, 7, 1, tzinfo=UTC),
        chat=chat or _chat(),
        from_user=_value(id=1001, username="owner", first_name="Owner"),
        sender_chat=None,
        message_thread_id=None,
        text=text,
        caption=caption,
        entities=None,
        caption_entities=None,
        reply_to_message=reply_to_message,
        forward_origin=forward_origin,
        forward_date=None,
        forward_from=None,
        forward_from_chat=None,
        forward_from_message_id=None,
        forward_sender_name=None,
        photo=photo,
        document=document,
    )


def _channel_origin():
    return _value(
        type="channel",
        date=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
        chat=_chat(-100300, chat_type="channel", username="news_channel"),
        message_id=44,
        sender_user=None,
        sender_user_name=None,
    )


def test_context_adapter_covers_supported_message_forms_and_bounds_payloads() -> None:
    forwarded = extract_message_context(
        _incoming(
            text="Пересланная заметка",
            forward_origin=_value(
                type="user",
                date=datetime(2026, 9, 8, 6, 0, tzinfo=UTC),
                sender_user=_value(id=77, username="source_user", first_name="Source"),
                sender_user_name=None,
            ),
        )
    )
    channel_post = extract_message_context(
        _incoming(text="Пост канала", forward_origin=_channel_origin())
    )
    reply = extract_message_context(
        _incoming(
            text="напомни об этом завтра в 9",
            reply_to_message=_source_message(text="Ответь на это сообщение"),
        )
    )
    link = extract_message_context(_incoming(text="Полезно: https://example.com/a"))
    photo = extract_message_context(
        _incoming(
            text=None,
            caption="Фото с подписью",
            photo=[_value(file_id="photo-file", width=100, height=100, file_size=12_000)],
        )
    )
    document = extract_message_context(
        _incoming(
            text=None,
            caption="Документ",
            document=_value(
                file_id="document-file",
                file_name="report.pdf",
                mime_type="application/pdf",
                file_size=42_000,
            ),
        )
    )
    ordinary = extract_message_context(_incoming(text="Обычное сообщение", forward_origin=None))

    assert forwarded is not None and forwarded.kind == ContextKind.FORWARDED.value
    assert forwarded.source_sender_label == "@source_user"
    assert channel_post is not None and channel_post.kind == ContextKind.CHANNEL_POST.value
    assert channel_post.source_chat_username == "news_channel"
    assert reply is not None and reply.kind == ContextKind.REPLY.value
    assert reply.source_message_id == 12
    assert link is not None and link.kind == ContextKind.LINK.value
    assert link.source_url == "https://example.com/a"
    assert photo is not None and photo.kind == ContextKind.PHOTO.value
    assert photo.media_file_id == "photo-file"
    assert document is not None and document.kind == ContextKind.DOCUMENT.value
    assert document.media_file_name == "report.pdf"
    assert ordinary is not None and ordinary.kind == ContextKind.ORDINARY.value
    assert not is_contextual_message(_incoming(text="Обычное сообщение"))

    bounded = extract_message_context(
        _incoming(
            text="x" * 20_000,
            forward_origin=_channel_origin(),
        )
    )
    assert bounded is not None
    assert len(bounded.source_text or "") == 1200
    assert len(serialize_context_snapshot(bounded).encode("utf-8")) <= 16_384


def test_context_parser_supports_ob_etom_and_short_schedule_answers() -> None:
    parsed = parse_context_reminder_input(
        "напомни об этом завтра в 9",
        now_local=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
    )
    assert parsed is not None
    assert parsed.text == CONTEXT_PLACEHOLDER

    resolved = parse_context_clarification_answer(
        "напомни об этом",
        "через 2 часа",
        now_local=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
    )
    assert resolved is not None
    assert resolved.text == CONTEXT_PLACEHOLDER
    assert resolved.datetime_semantics == "instant"

    time_only = parse_context_clarification_answer(
        "напомни об этом",
        "09:00",
        now_local=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
    )
    assert time_only is not None
    assert time_only.local_dt == datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


def test_context_serialization_rejects_malformed_or_unsafe_urls() -> None:
    snapshot = MessageContextSnapshot(
        kind="link",
        source_chat_id=2002,
        source_message_id=12,
        source_url="javascript:alert(1)",
        source_text="https://example.com",
    )
    serialized = serialize_context_snapshot(snapshot)
    restored = deserialize_context_snapshot(serialized)
    assert restored is not None
    assert restored.source_url is None
    assert deserialize_context_snapshot("not-json") is None
    assert deserialize_context_snapshot('{"kind":"link","source_chat_id":"secret"}') is None
    assert "alert(1)" not in format_context_for_delivery(restored)


def _open_sqlite(monkeypatch):
    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        connection = await engine.connect()
        await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        for module in (
            reminder_service,
            clarification_service,
            message_context,
            voice_service,
            worker,
        ):
            monkeypatch.setattr(module, "SessionLocal", session_factory)
        user = User(telegram_user_id=1001, chat_id=2002, timezone="Europe/Moscow")
        async with session_factory() as session:
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return engine, connection, session_factory, user

    return setup


def test_handler_persists_reply_context_and_returns_clear_preview(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory, user = await _open_sqlite(monkeypatch)()
        try:

            async def get_user(*, telegram_user_id: int, chat_id: int):
                assert (telegram_user_id, chat_id) == (1001, 2002)
                return user

            monkeypatch.setattr(reminders_handler, "get_or_create_user", get_user)
            source = _source_message(
                text="Оплатить счёт", chat=_chat(-10077, chat_type="supergroup")
            )
            message = _incoming(
                text="напомни об этом завтра в 9",
                reply_to_message=source,
            )
            message.answer = AsyncMock()

            await reminders_handler._create_and_answer(message)

            async with session_factory() as session:
                reminder = await session.scalar(select(Reminder).where(Reminder.user_id == user.id))
                context = await session.scalar(select(ReminderContext))
            assert reminder is not None
            assert reminder.text == "Оплатить счёт"
            assert reminder.context_kind == ContextKind.REPLY.value
            assert context is not None
            assert context.source_chat_id == -10077
            assert context.source_message_id == 12
            assert "Оплатить счёт" in message.answer.await_args.args[0]
            assert "Сообщение" in message.answer.await_args.args[0]
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_forward_without_schedule_is_restart_safe_and_then_creates_context_reminder(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, connection, session_factory, user = await _open_sqlite(monkeypatch)()
        try:

            async def get_user(*, telegram_user_id: int, chat_id: int):
                return user

            monkeypatch.setattr(reminders_handler, "get_or_create_user", get_user)
            message = _incoming(
                text="Проверить отчёт",
                forward_origin=_channel_origin(),
            )
            message.answer = AsyncMock()

            await reminders_handler._create_and_answer(message)

            async with session_factory() as session:
                clarification = await session.scalar(select(ReminderClarification))
                assert clarification is not None
                stored_snapshot = deserialize_context_snapshot(clarification.context_snapshot)
            assert stored_snapshot is not None
            assert stored_snapshot.kind == ContextKind.CHANNEL_POST.value
            assert "Контекст сохранён" in message.answer.await_args.args[0]

            answer = _value(text="завтра в 9", caption=None, answer=AsyncMock())
            assert await reminders_handler._handle_clarification(answer, user)

            async with session_factory() as session:
                reminders = list((await session.scalars(select(Reminder))).all())
                clarifications = list((await session.scalars(select(ReminderClarification))).all())
                context = await session.scalar(select(ReminderContext))
            assert len(reminders) == 1
            assert reminders[0].text == "Проверить отчёт"
            assert reminders[0].context_kind == ContextKind.CHANNEL_POST.value
            assert context is not None and context.source_message_id == 44
            assert clarifications == []
            assert answer.answer.await_count == 1
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_context_ownership_retention_and_missing_source_fallback(monkeypatch) -> None:
    async def scenario() -> None:
        engine, connection, session_factory, user = await _open_sqlite(monkeypatch)()
        try:
            now = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
            async with session_factory() as session:
                reminder = Reminder(
                    user_id=user.id,
                    chat_id=user.chat_id,
                    text="Сохранить контекст",
                    remind_at_utc=now + timedelta(hours=1),
                    delivery_at_utc=now + timedelta(hours=1),
                    schedule_timezone=user.timezone,
                    context_kind=ContextKind.FORWARDED.value,
                )
                session.add(reminder)
                await session.flush()
                session.add(
                    ReminderContext(
                        reminder_id=reminder.id,
                        user_id=user.id,
                        chat_id=user.chat_id,
                        kind=ContextKind.FORWARDED.value,
                        source_chat_id=-10077,
                        source_message_id=12,
                        source_text="Приватный текст",
                        expires_at=now - timedelta(seconds=1),
                    )
                )
                await session.commit()
                reminder_id = reminder.id

            assert (
                await get_context_for_delivery(
                    reminder_id,
                    user.id,
                    user.chat_id,
                    now_utc=now,
                )
                is None
            )
            assert await cleanup_expired_reminder_contexts(now_utc=now) == 1
            async with session_factory() as session:
                assert await session.scalar(select(func.count()).select_from(ReminderContext)) == 0

            assert "истекло или недоступно" in format_context_for_delivery(
                None,
                fallback_kind=ContextKind.FORWARDED.value,
            )
            other_user = User(telegram_user_id=3003, chat_id=4004, timezone="UTC")
            async with session_factory() as session:
                session.add(other_user)
                await session.commit()
                await session.refresh(other_user)
            assert (
                await get_context_for_delivery(
                    reminder_id,
                    other_user.id,
                    other_user.chat_id,
                    now_utc=now,
                )
                is None
            )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_worker_prefers_file_reference_and_falls_back_when_media_is_deleted(monkeypatch) -> None:
    class FakeBot:
        def __init__(self, *, fail_media: bool = False) -> None:
            self.fail_media = fail_media
            self.photo_calls: list[dict] = []
            self.message_calls: list[dict] = []

        async def send_photo(self, **kwargs):
            self.photo_calls.append(kwargs)
            if self.fail_media:
                raise RuntimeError("missing source")
            return _value(message_id=901)

        async def send_message(self, **kwargs):
            self.message_calls.append(kwargs)
            return _value(message_id=902)

    async def scenario() -> None:
        reminder = Reminder(
            id=7,
            user_id=1,
            chat_id=2,
            text="Открыть фото",
            remind_at_utc=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            delivery_at_utc=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            schedule_timezone="UTC",
            context_kind=ContextKind.PHOTO.value,
        )
        context = MessageContextSnapshot(
            kind=ContextKind.PHOTO.value,
            source_chat_id=2,
            source_message_id=55,
            source_text="Фото с планом",
            media_kind="photo",
            media_file_id="photo-file-id",
        )

        first_bot = FakeBot()
        await worker._send_delivery(
            first_bot,
            reminder,
            context,
            reply_markup=worker.reminder_actions_kb(reminder.id),
        )
        assert first_bot.photo_calls[0]["photo"] == "photo-file-id"
        assert first_bot.message_calls == []

        class MissingMediaError(Exception):
            pass

        monkeypatch.setattr(worker, "TelegramBadRequest", MissingMediaError)
        fallback_bot = FakeBot(fail_media=True)

        # Rebind the fake failure to the class used by the worker's narrow
        # deleted/unavailable-source fallback branch.
        async def fail_media(**kwargs):
            fallback_bot.photo_calls.append(kwargs)
            raise MissingMediaError()

        fallback_bot.send_photo = fail_media
        await worker._send_delivery(
            fallback_bot,
            reminder,
            context,
            reply_markup=worker.reminder_actions_kb(reminder.id),
        )
        assert len(fallback_bot.message_calls) == 1
        assert "недоступно" in fallback_bot.message_calls[0]["text"]

    asyncio.run(scenario())


def test_context_reminder_text_uses_media_label_for_command_caption() -> None:
    snapshot = MessageContextSnapshot(
        kind=ContextKind.PHOTO.value,
        media_kind="photo",
        source_caption="напомни завтра в 9",
    )
    assert context_reminder_text(snapshot) == "Фото из Telegram"
