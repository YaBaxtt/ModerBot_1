from __future__ import annotations

from collections.abc import Awaitable, Callable
from html import escape
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.database.models import Chat
from app.services.subscriptions import missing_subscriptions, record_impressions, subscription_enabled, subscription_links
from app.services.text import user_label
from app.services.users import upsert_user


def subscription_keyboard(links, check_data: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f'📢 {link.title[:55]}', url=link.url)] for link in links]
    rows.append([InlineKeyboardButton(text='✅ Проверить подписку', callback_data=check_data, style=ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_text(links, *, group: bool) -> str:
    descriptions = [f'• <b>{escape(link.title)}</b>' + (f' — {escape(link.description)}' if link.description else '') for link in links]
    action = 'Чтобы писать в этой группе' if group else 'Чтобы пользоваться ботом'
    return (
        '📢 <b>ОБЯЗАТЕЛЬНАЯ ПОДПИСКА</b>\n'
        '━━━━━━━━━━━━\n\n'
        f'{action}, подпишитесь на указанные каналы и чаты:\n\n'
        + '\n'.join(descriptions)
        + '\n\nПосле подписки нажмите <b>«Проверить подписку»</b>.'
    )


class RequiredSubscriptionMiddleware(BaseMiddleware):
    """Gate private bot access and group messages behind configured subscriptions."""

    def __init__(self) -> None:
        self._group_prompts: dict[tuple[int, int], float] = {}

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        session, bot, config = data.get('session'), data.get('bot'), data.get('config')
        if not session or not bot or not config:
            return await handler(event, data)
        if getattr(event, 'pre_checkout_query', None):
            return await handler(event, data)
        if (data.get('raw_state') or '').startswith('SubscriptionForm:'):
            return await handler(event, data)
        callback = getattr(event, 'callback_query', None)
        if callback and (callback.data or '').startswith(('subcheck:', 'subcfg:')):
            return await handler(event, data)
        message = getattr(event, 'message', None)
        if message and getattr(message, 'successful_payment', None):
            return await handler(event, data)
        actor = callback.from_user if callback else (message.from_user if message else None)
        if not actor or actor.is_bot or config.is_owner(actor.id):
            return await handler(event, data)

        effective_message = callback.message if callback else message
        if not effective_message:
            return await handler(event, data)
        if effective_message.chat.type == 'private':
            if not await subscription_enabled(session, None):
                return await handler(event, data)
            links = await subscription_links(session, None)
            if not links:
                return await handler(event, data)
            missing = await missing_subscriptions(bot, links, actor.id)
            if not missing:
                return await handler(event, data)
            user = await upsert_user(session, actor)
            await record_impressions(session, missing, user)
            if callback:
                await callback.answer('Сначала подтвердите обязательную подписку.', show_alert=True)
                await callback.message.answer(subscription_text(missing, group=False), reply_markup=subscription_keyboard(missing, 'subcheck:private'))
            else:
                await message.answer(subscription_text(missing, group=False), reply_markup=subscription_keyboard(missing, 'subcheck:private'))
            return None

        if effective_message.chat.type not in {'group', 'supergroup'} or callback:
            return await handler(event, data)
        chat = await session.scalar(select(Chat).where(Chat.telegram_id == effective_message.chat.id))
        if not chat or not await subscription_enabled(session, chat.id):
            return await handler(event, data)
        try:
            local_member = await bot.get_chat_member(effective_message.chat.id, actor.id)
            if local_member.status in {'administrator', 'creator'}:
                return await handler(event, data)
        except TelegramAPIError:
            pass
        links = await subscription_links(session, chat.id)
        if not links:
            return await handler(event, data)
        missing = await missing_subscriptions(bot, links, actor.id)
        if not missing:
            return await handler(event, data)
        try:
            await bot.delete_message(effective_message.chat.id, effective_message.message_id)
        except TelegramAPIError:
            pass
        key = (effective_message.chat.id, actor.id)
        now = monotonic()
        if now - self._group_prompts.get(key, 0) >= 60:
            self._group_prompts[key] = now
            user = await upsert_user(session, actor)
            await record_impressions(session, missing, user)
            try:
                await bot.send_message(
                    effective_message.chat.id,
                    f'{user_label(actor)}\n\n{subscription_text(missing, group=True)}',
                    reply_markup=subscription_keyboard(missing, f'subcheck:group:{chat.id}:{actor.id}'),
                )
            except TelegramAPIError:
                pass
        return None
