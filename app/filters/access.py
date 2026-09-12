from __future__ import annotations

from aiogram import Bot
from aiogram.filters import BaseFilter
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat
from app.services.moderators import can_moderate_chat


class OwnerOnly(BaseFilter):
    async def __call__(self, event_from_user, config: Settings, **_: object) -> bool:
        return bool(event_from_user and config.is_owner(event_from_user.id))


async def is_moderator(bot: Bot, session: AsyncSession, chat_telegram_id: int, user_id: int, config: Settings) -> bool:
    chat = await session.scalar(select(Chat).where(Chat.telegram_id == chat_telegram_id))
    if not chat:
        return config.is_owner(user_id)
    return await can_moderate_chat(bot, session, chat, user_id, config)
