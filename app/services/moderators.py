from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatModerator, User


async def telegram_role(bot, chat_telegram_id: int, user_telegram_id: int) -> str | None:
    try:
        return (await bot.get_chat_member(chat_telegram_id, user_telegram_id)).status
    except TelegramAPIError:
        return None


async def can_manage_chat(bot, chat: Chat, user_telegram_id: int, config: Settings) -> bool:
    """Only the Telegram chat creator (or bot owner) may delegate access."""
    if config.is_owner(user_telegram_id):
        return True
    return await telegram_role(bot, chat.telegram_id, user_telegram_id) == 'creator'


async def active_assignment(session: AsyncSession, chat_id: int, user_telegram_id: int) -> ChatModerator | None:
    return await session.scalar(
        select(ChatModerator)
        .join(User, User.id == ChatModerator.user_id)
        .where(ChatModerator.chat_id == chat_id, User.telegram_id == user_telegram_id, ChatModerator.is_active.is_(True))
    )


async def can_moderate_chat(bot, session: AsyncSession, chat: Chat, user_telegram_id: int, config: Settings) -> bool:
    if await can_manage_chat(bot, chat, user_telegram_id, config):
        return True
    assignment = await active_assignment(session, chat.id, user_telegram_id)
    if not assignment:
        return False
    # A removed member must not keep remote moderation access.
    role = await telegram_role(bot, chat.telegram_id, user_telegram_id)
    return role not in {None, 'left', 'kicked'}


async def set_moderator(session: AsyncSession, *, chat_id: int, user_id: int, granted_by_user_id: int) -> ChatModerator:
    row = await session.scalar(select(ChatModerator).where(ChatModerator.chat_id == chat_id, ChatModerator.user_id == user_id))
    now = datetime.now(timezone.utc)
    if row:
        row.granted_by_user_id = granted_by_user_id
        row.granted_at = now
        row.revoked_at = None
        row.is_active = True
    else:
        row = ChatModerator(chat_id=chat_id, user_id=user_id, granted_by_user_id=granted_by_user_id, granted_at=now, is_active=True)
        session.add(row)
    return row


async def revoke_moderator(session: AsyncSession, *, chat_id: int, user_id: int) -> bool:
    row = await session.scalar(select(ChatModerator).where(ChatModerator.chat_id == chat_id, ChatModerator.user_id == user_id, ChatModerator.is_active.is_(True)))
    if not row:
        return False
    row.is_active = False
    row.revoked_at = datetime.now(timezone.utc)
    return True
