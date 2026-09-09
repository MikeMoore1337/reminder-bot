import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.services import timezone_service


def test_default_and_explicit_user_timezone_survive_new_sessions(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(timezone_service, "SessionLocal", session_factory)

            created = await timezone_service.get_or_create_user(
                telegram_user_id=3003,
                chat_id=4004,
            )
            assert created.timezone == "Europe/Moscow"

            selected = await timezone_service.set_user_timezone(
                telegram_user_id=3003,
                chat_id=4004,
                timezone_name="Europe/Helsinki",
            )
            assert selected.timezone == "Europe/Helsinki"

            reloaded = await timezone_service.get_or_create_user(
                telegram_user_id=3003,
                chat_id=5005,
            )
            assert reloaded.timezone == "Europe/Helsinki"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_group_chat_cannot_replace_private_user_destination(monkeypatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(timezone_service, "SessionLocal", session_factory)

            private = await timezone_service.get_or_create_user(
                telegram_user_id=3103,
                chat_id=4103,
            )
            preserved = await timezone_service.get_or_create_user(
                telegram_user_id=3103,
                chat_id=-1004103,
            )
            assert preserved.chat_id == private.chat_id == 4103

            updated = await timezone_service.set_user_timezone(
                telegram_user_id=3103,
                chat_id=-1004103,
                timezone_name="Europe/Helsinki",
            )
            assert updated.chat_id == 4103
            assert updated.timezone == "Europe/Helsinki"

            group_user = await timezone_service.get_or_create_user(
                telegram_user_id=3104,
                chat_id=-1004104,
            )
            assert group_user.chat_id == -1004104
            upgraded = await timezone_service.get_or_create_user(
                telegram_user_id=3104,
                chat_id=4104,
            )
            assert upgraded.chat_id == 4104
        finally:
            await engine.dispose()

    asyncio.run(scenario())
