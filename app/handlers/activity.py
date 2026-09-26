import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ModerationAction, ModerationActionType, Warning
from app.services.antispam import AntiSpamGuard, SpamDecision, message_signature
from app.services.activity import record_message_activity
from app.services.parse import parse_duration
from app.services.users import upsert_chat, upsert_user
from app.services.features import feature_states
from app.services.text import user_label
from app.services.protections import protection_action, protection_states

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


async def delete_spam_messages(bot: Bot, message: Message, decision: SpamDecision) -> int:
    message_ids = decision.message_ids or (message.message_id,)
    deleted = 0
    for message_id in message_ids:
        try:
            await bot.delete_message(message.chat.id, message_id)
            deleted += 1
        except TelegramAPIError:
            logging.getLogger(__name__).warning('Could not delete spam message %s', message_id, exc_info=True)
    return deleted


async def punish_spammer(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    config: Settings,
    chat: Chat,
    action: str,
    reason: str,
) -> None:
    user = await upsert_user(session, message.from_user)
    automatic_reason = f'Автоматически: {reason}'
    outcome = ''
    try:
        if action == 'warn':
            session.add(Warning(chat_id=chat.id, target_user_id=user.id, moderator_user_id=None, reason=automatic_reason))
            await session.flush()
            count = await session.scalar(select(func.count(Warning.id)).where(
                Warning.chat_id == chat.id,
                Warning.target_user_id == user.id,
                Warning.is_active.is_(True),
            )) or 0
            session.add(ModerationAction(
                chat_id=chat.id, target_user_id=user.id, moderator_user_id=None,
                action=ModerationActionType.WARN, reason=automatic_reason,
            ))
            outcome = f'предупреждение {count}/3'
            if count >= 3:
                await bot.ban_chat_member(chat.telegram_id, user.telegram_id)
                session.add(ModerationAction(
                    chat_id=chat.id, target_user_id=user.id, moderator_user_id=None,
                    action=ModerationActionType.BAN, reason='Автоматически: 3 предупреждения',
                ))
                outcome = 'бан за 3/3 предупреждения'
        elif action == 'ban':
            await bot.ban_chat_member(chat.telegram_id, user.telegram_id)
            session.add(ModerationAction(
                chat_id=chat.id, target_user_id=user.id, moderator_user_id=None,
                action=ModerationActionType.BAN, reason=automatic_reason,
            ))
            outcome = 'бан'
        else:
            duration = parse_duration(config.antispam_mute_duration) or timedelta(hours=1)
            await bot.restrict_chat_member(
                chat.telegram_id,
                user.telegram_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now(timezone.utc) + duration,
            )
            session.add(ModerationAction(
                chat_id=chat.id, target_user_id=user.id, moderator_user_id=None,
                action=ModerationActionType.MUTE, reason=automatic_reason,
                duration_seconds=int(duration.total_seconds()),
            ))
            outcome = f'мут на {config.antispam_mute_duration}'
    except TelegramAPIError:
        logging.getLogger(__name__).warning('Could not punish spammer', exc_info=True)
        outcome = 'сообщения удалены, но Telegram не разрешил применить наказание'

    await message.answer(
        '🛡 <b>АНТИСПАМ СРАБОТАЛ</b>\n'
        '━━━━━━━━━━━━\n\n'
        f'👤 Пользователь: {user_label(user)}\n'
        f'📝 Причина: <b>{escape(reason)}</b>\n'
        f'⚖️ Действие: <b>{escape(outcome)}</b>.'
    )


@router.message(F.chat.type.in_({"group", "supergroup"}), F.from_user.is_bot == False, ~F.text.startswith("/"))
async def activity(message: Message, session: AsyncSession, bot: Bot, config: Settings) -> None:
    features = await feature_states(session, ('antispam', 'points'))
    chat = await upsert_chat(session, message.chat)
    chat_settings = await protection_states(session, chat.id)
    if await is_exempt(message, bot, config):
        await record_message_activity(session, message, award_points=features['points'])
        return
    if not features['antispam'] or not chat_settings['antispam']:
        await record_message_activity(session, message, award_points=features['points'])
        return
    guard.burst_limit = config.antispam_burst_limit
    guard.burst_window = config.antispam_burst_window_seconds
    guard.repeat_limit = config.antispam_identical_limit
    guard.repeat_window = config.antispam_identical_window_seconds
    decision = guard.inspect(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        signature=message_signature(message),
        message_id=message.message_id,
        now=None,
    )
    if decision.delete:
        await delete_spam_messages(bot, message, decision)
        if decision.punish:
            action = await protection_action(session, chat.id, 'antispam')
            await punish_spammer(message, session, bot, config, chat, action, decision.reason or 'спам')
        await record_message_activity(session, message, award_points=False)
        return
    await record_message_activity(session, message, award_points=features['points'])
