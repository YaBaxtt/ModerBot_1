"""Observe profile changes whenever Telegram delivers activity from a member."""
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError

from app.services.features import feature_enabled
from app.services.member_tracking import track_member_profile


log = logging.getLogger(__name__)


class MemberTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, update, data):
        session = data.get('session')
        bot = data.get('bot')
        telegram_user = telegram_chat = None
        message = update.message or update.edited_message
        if message and message.chat.type in {'group', 'supergroup'}:
            telegram_user, telegram_chat = message.from_user, message.chat
        elif update.callback_query and update.callback_query.message and update.callback_query.message.chat.type in {'group', 'supergroup'}:
            telegram_user, telegram_chat = update.callback_query.from_user, update.callback_query.message.chat
        elif update.chat_member:
            telegram_user, telegram_chat = update.chat_member.new_chat_member.user, update.chat_member.chat
        if session and bot and telegram_user and not telegram_user.is_bot and await feature_enabled(session, 'member_tracking'):
            try:
                notification = await track_member_profile(bot, session, telegram_user, telegram_chat)
                if notification:
                    await bot.send_message(telegram_chat.id, notification, disable_notification=True)
            except TelegramAPIError:
                log.warning('Could not announce member profile update in %s', telegram_chat.id)
            except Exception:
                # Profile tracking must never break moderation or the user's update.
                log.exception('Member profile tracking failed in %s', telegram_chat.id)
        return await handler(update, data)
