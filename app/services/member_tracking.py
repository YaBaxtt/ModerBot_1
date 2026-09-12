from datetime import datetime, timedelta, timezone
from html import escape
import logging

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MemberProfileSnapshot
from app.services.users import upsert_chat, upsert_user


log = logging.getLogger(__name__)
# Telegram has no avatar-change update. Poll on member activity, but keep a
# small per-user cooldown so an active chat does not hit the Bot API each time.
AVATAR_CHECK_INTERVAL = timedelta(seconds=30)


def plain_username(value: str | None) -> str:
    return f'<code>@{escape(value)}</code>' if value else '<code>нет</code>'


def plain_name(first: str, last: str | None) -> str:
    return escape(' '.join(filter(None, (first, last))))


async def current_avatar(bot, telegram_id: int) -> tuple[bool, str | None]:
    try:
        photos = await bot.get_user_profile_photos(telegram_id, limit=1)
        if not photos.photos:
            return True, None
        return True, photos.photos[0][0].file_unique_id
    except TelegramAPIError:
        return False, None


async def track_member_profile(bot, session: AsyncSession, telegram_user, telegram_chat) -> str | None:
    user = await upsert_user(session, telegram_user)
    chat = await upsert_chat(session, telegram_chat)
    snapshot = await session.scalar(select(MemberProfileSnapshot).where(MemberProfileSnapshot.user_id == user.id, MemberProfileSnapshot.chat_id == chat.id))
    now = datetime.now(timezone.utc)
    should_check_avatar = not snapshot or not snapshot.avatar_checked_at or now.replace(tzinfo=None) - snapshot.avatar_checked_at.replace(tzinfo=None) >= AVATAR_CHECK_INTERVAL
    avatar_ok, avatar = await current_avatar(bot, telegram_user.id) if should_check_avatar else (False, None)
    if not snapshot:
        session.add(MemberProfileSnapshot(
            user_id=user.id,
            chat_id=chat.id,
            first_name=telegram_user.first_name or 'Пользователь',
            last_name=telegram_user.last_name,
            username=telegram_user.username,
            avatar_file_unique_id=avatar if avatar_ok else None,
            avatar_known=avatar_ok,
            avatar_checked_at=now,
            updated_at=now,
        ))
        return None
    changes = []
    old_name = plain_name(snapshot.first_name, snapshot.last_name)
    new_name = plain_name(telegram_user.first_name or 'Пользователь', telegram_user.last_name)
    if (snapshot.first_name, snapshot.last_name) != (telegram_user.first_name or 'Пользователь', telegram_user.last_name):
        changes.append(f'✏️ Имя: {old_name} → <b>{new_name}</b>')
    if snapshot.username != telegram_user.username:
        changes.append(f'🔗 Username: {plain_username(snapshot.username)} → {plain_username(telegram_user.username)}')
    if avatar_ok and snapshot.avatar_known and snapshot.avatar_file_unique_id != avatar:
        changes.append('🖼 Аватарка обновлена')
    snapshot.first_name = telegram_user.first_name or 'Пользователь'
    snapshot.last_name = telegram_user.last_name
    snapshot.username = telegram_user.username
    if should_check_avatar:
        snapshot.avatar_checked_at = now
    if avatar_ok:
        snapshot.avatar_file_unique_id = avatar
        snapshot.avatar_known = True
    snapshot.updated_at = now
    if not changes:
        return None
    identity = f'<a href="tg://user?id={telegram_user.id}">{new_name}</a>'
    return f'🔔 <b>ОБНОВЛЕНИЕ ПРОФИЛЯ</b>\n━━━━━━━━━━━━\n\n👤 {identity}\n' + '\n'.join(changes)
