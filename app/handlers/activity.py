import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import ChatPermissions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ModerationAction, ModerationActionType
from app.services.antispam import AntiSpamGuard
from app.services.activity import record_message_activity
from app.services.parse import parse_duration
from app.services.users import upsert_chat, upsert_user
from app.services.features import feature_states
from app.services.text import user_label
from app.services.protections import protection_states

router = Router(name="activity")
guard = AntiSpamGuard()


async def is_exempt(message: Message, bot: Bot, config: Settings) -> bool:
    if config.is_owner(message.from_user.id):
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


@router.message(F.chat.type.in_({"group", "supergroup"}), F.from_user.is_bot == False, ~F.text.startswith("/"))
async def activity(message: Message, session: AsyncSession, bot: Bot, config: Settings) -> None:
    features = await feature_states(session, ('antispam', 'points'))
    chat_settings = await protection_states(session, (await upsert_chat(session, message.chat)).id)
    if await is_exempt(message, bot, config):
        await record_message_activity(session, message, award_points=features['points'])
        return
    if not features['antispam'] or not chat_settings['antispam']:
        await record_message_activity(session, message, award_points=features['points'])
        return
    guard.min_interval = config.antispam_min_interval_seconds
    guard.repeat_limit = config.antispam_repeat_limit
    guard.repeat_window = config.antispam_repeat_window_seconds
    decision = guard.inspect(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=message.text or message.caption or "",
        now=None,
    )
    if decision.delete:
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            logging.getLogger(__name__).warning("Could not delete spam message", exc_info=True)
        if decision.mute:
            duration = parse_duration(config.antispam_mute_duration) or timedelta(hours=1)
            try:
                await bot.restrict_chat_member(
                    message.chat.id,
                    message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc) + duration,
                )
                user = await upsert_user(session, message.from_user)
                chat = await upsert_chat(session, message.chat)
                session.add(ModerationAction(chat_id=chat.id, target_user_id=user.id, moderator_user_id=None, action=ModerationActionType.MUTE, reason="Автоматически: 5 одинаковых сообщений", duration_seconds=int(duration.total_seconds())))
                await message.answer(f"🔇 <b>АВТОМУТ ЗА СПАМ</b>\n━━━━━━━━━━━━\n\n👤 Пользователь: {user_label(user)}\n⏱ Срок: <b>{escape(config.antispam_mute_duration)}</b>\n📝 Причина: <b>{config.antispam_repeat_limit} одинаковых сообщений подряд</b>.")
            except Exception:
                logging.getLogger(__name__).warning("Could not mute repeated spammer", exc_info=True)
                await message.answer(f"⚠️ Не удалось автоматически замьютить {user_label(message.from_user)}. Проверьте право бота ограничивать участников.")
        return
    await record_message_activity(session, message, award_points=features['points'])
