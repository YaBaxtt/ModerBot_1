from __future__ import annotations

from datetime import datetime, timezone

from aiogram.types import Chat as TelegramChat
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Chat, User


async def upsert_user(session: AsyncSession, telegram_user: TelegramUser) -> User:
    now = datetime.now(timezone.utc)
    statement = insert(User).values(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name or "Пользователь",
        last_name=telegram_user.last_name,
        last_seen_at=now,
    ).on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "username": telegram_user.username,
            "first_name": telegram_user.first_name or "Пользователь",
            "last_name": telegram_user.last_name,
            "last_seen_at": now,
        },
    )
    await session.execute(statement)
    return await session.scalar(select(User).where(User.telegram_id == telegram_user.id))  # type: ignore[return-value]


async def upsert_chat(session: AsyncSession, telegram_chat: TelegramChat) -> Chat:
    title = telegram_chat.title or telegram_chat.full_name or str(telegram_chat.id)
    statement = insert(Chat).values(
        telegram_id=telegram_chat.id, title=title, username=telegram_chat.username, is_active=True
    ).on_conflict_do_update(
        index_elements=[Chat.telegram_id],
        set_={"title": title, "username": telegram_chat.username, "is_active": True},
    )
    await session.execute(statement)
    return await session.scalar(select(Chat).where(Chat.telegram_id == telegram_chat.id))  # type: ignore[return-value]


async def find_user(session: AsyncSession, raw_target: str) -> User | None:
    cleaned = raw_target.strip()
    if cleaned.startswith("@"):
        return await session.scalar(select(User).where(User.username.ilike(cleaned[1:])))
    if cleaned.lstrip("-").isdigit():
        return await session.scalar(select(User).where(User.telegram_id == int(cleaned)))
    return None

