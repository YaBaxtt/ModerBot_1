"""Remove slash-command messages from groups to keep chats clean."""
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from app.services.features import feature_enabled


log = logging.getLogger(__name__)


class CommandCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, update, data):
        message = update.message
        source = (message.text or message.caption or '').lstrip() if message else ''
        if message and message.chat.type in {'group', 'supergroup'} and source.startswith('/'):
            session = data.get('session')
            cleanup_enabled = not session or await feature_enabled(session, 'command_cleanup')
            if not cleanup_enabled:
                return await handler(update, data)
            try:
                await message.delete()
                data['group_command_deleted'] = True
            except TelegramAPIError:
                # The command still runs; health check explains the missing right.
                log.debug('Could not delete group command in %s', message.chat.id)
        return await handler(update, data)
