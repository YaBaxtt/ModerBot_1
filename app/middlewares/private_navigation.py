"""Keep a menu exit on every private interactive screen, not on broadcasts."""
import asyncio
from contextvars import ContextVar

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Task identity prevents inherited background-job contexts decorating broadcasts.
navigation_context = ContextVar('navigation_context', default=None)


class PrivateNavigationMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        context = navigation_context.get()
        if context and context[0] is asyncio.current_task() and getattr(method, 'chat_id', None) == context[1]:
            if method.__api_method__ in {'sendMessage', 'sendPhoto', 'sendVideo', 'editMessageText', 'editMessageCaption', 'editMessageReplyMarkup'}:
                markup = getattr(method, 'reply_markup', None)
                if markup is None or isinstance(markup, InlineKeyboardMarkup):
                    rows = list(markup.inline_keyboard) if markup else []
                    exits = {'nav:private_main', 'menu:home', 'admin:home', 'moder:home'}
                    if not any(button.callback_data in exits for row in rows for button in row):
                        rows.append([InlineKeyboardButton(text='⬅️ В меню', callback_data=context[2])])
                        method = method.model_copy(update={'reply_markup': InlineKeyboardMarkup(inline_keyboard=rows)})
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            # After a short network outage Telegram may deliver button presses
            # whose acknowledgement window has already expired. Acknowledging
            # is optional; swallowing only this error lets the actual callback
            # handler continue (edit the screen, cancel a form, go back, etc.).
            if method.__api_method__ == 'answerCallbackQuery' and (
                'query is too old' in exc.message.lower()
                or 'query id is invalid' in exc.message.lower()
            ):
                return True
            raise
