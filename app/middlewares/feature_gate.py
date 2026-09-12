"""Stop disabled user-facing flows at one consistent boundary."""
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import EphemeralMessageParameters

from app.keyboards.common import back_button
from app.services.features import FEATURE_BY_KEY, feature_enabled


CALLBACK_FEATURES = {
    'top:': 'leaderboard',
    'group:top': 'leaderboard',
    'shop:': 'shop',
    'ads:': 'advertising',
    'broadcast:': 'broadcasts',
    'announce:': 'announcements',
    'daily:': 'daily_reward',
    'moder:': 'moderation',
}
COMMAND_FEATURES = {
    '/top': 'leaderboard', '/shop': 'shop', '/report': 'reports', '/daily': 'daily_reward',
    '/ban': 'moderation', '/unban': 'moderation', '/mute': 'moderation', '/unmute': 'moderation',
    '/warn': 'moderation', '/unwarn': 'moderation', '/history': 'moderation', '/moder': 'moderation',
}
STATE_FEATURES = {
    'PrivateReportForm:': 'reports', 'AdForm:': 'advertising',
    'BroadcastForm:': 'broadcasts', 'AnnouncementForm:': 'announcements',
}


class FeatureGateMiddleware(BaseMiddleware):
    async def __call__(self, handler, update, data):
        session = data.get('session')
        if not session:
            return await handler(update, data)
        callback, message = update.callback_query, update.message
        feature = None
        if callback:
            value = callback.data or ''
            if value == 'menu:report_help' or value == 'private_report:send' or value == 'private_report:media_done':
                feature = 'reports'
            else:
                feature = next((key for prefix, key in CALLBACK_FEATURES.items() if value.startswith(prefix)), None)
        elif message and message.text:
            command = message.text.split(maxsplit=1)[0].lower().split('@')[0]
            feature = COMMAND_FEATURES.get(command)
        if not feature:
            raw_state = data.get('raw_state') or ''
            feature = next((key for prefix, key in STATE_FEATURES.items() if raw_state.startswith(prefix)), None)
        if not feature or await feature_enabled(session, feature):
            return await handler(update, data)
        state = data.get('state')
        if state:
            await state.clear()
            data['raw_state'] = None
        title = FEATURE_BY_KEY[feature].title
        if callback:
            await callback.answer(f'«{title}» сейчас отключена владельцем.', show_alert=True)
        elif message:
            text = f'⛔ <b>{title}</b> сейчас отключена владельцем бота.'
            if message.chat.type == 'private':
                await message.answer(text, reply_markup=back_button('nav:private_main'))
            else:
                try:
                    await data['bot'].send_message(message.chat.id, text, ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=message.from_user.id))
                except TelegramAPIError:
                    pass
