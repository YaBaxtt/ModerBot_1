from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from html import escape
import re
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from app.database.models import Chat, MemberEvent, ModerationAction, ModerationActionType, Warning
from app.services.protections import forbidden_words, protection_action, protection_states
from app.services.text import user_label
from app.services.users import upsert_user


MEDIA_FIELDS = (
    'photo', 'video', 'animation', 'audio', 'voice', 'video_note', 'document',
    'sticker', 'story', 'poll', 'dice', 'game', 'contact', 'location', 'venue',
)
ADULT_TERMS = ('порно', 'хентай', 'porn', 'hentai', 'xxx', 'onlyfans', '18+')


class PremiumProtectionMiddleware(BaseMiddleware):
    """Apply group-scoped PRO policies before normal update handlers."""

    def __init__(self) -> None:
        self._joins: dict[int, deque[tuple[int, float]]] = defaultdict(deque)
        self._sos_calls: dict[tuple[int, int], float] = {}

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        session = data.get('session')
        bot = data.get('bot')
        config = data.get('config')
        if not session or not bot:
            return await handler(event, data)

        message = getattr(event, 'message', None)
        if message and message.chat.type in {'group', 'supergroup'}:
            chat = await session.scalar(select(Chat).where(Chat.telegram_id == message.chat.id))
            if chat:
                states = await protection_states(session, chat.id)
                if await self._protect_message(message, bot, config, session, chat, states):
                    return None

        member_event = getattr(event, 'chat_member', None)
        if member_event and member_event.chat.type in {'group', 'supergroup'}:
            chat = await session.scalar(select(Chat).where(Chat.telegram_id == member_event.chat.id))
            if chat:
                states = await protection_states(session, chat.id)
                await self._record_member_event(event, member_event, session, chat)
                if await self._protect_membership(member_event, bot, chat, states):
                    return None

        return await handler(event, data)

    @staticmethod
    async def _delete(bot, message) -> bool:
        try:
            await bot.delete_message(message.chat.id, message.message_id)
            return True
        except TelegramAPIError:
            return False

    async def _protect_message(self, message, bot, config, session, chat, states: dict[str, bool]) -> bool:
        text = message.text or message.caption or ''
        if states['sos_admin'] and re.search(r'(?<!\w)@admin\b', text, re.IGNORECASE):
            caller_id = getattr(message.from_user, 'id', 0) or getattr(message.sender_chat, 'id', 0)
            cooldown_key = (message.chat.id, caller_id)
            now = monotonic()
            last_call = self._sos_calls.get(cooldown_key)
            # A newly started Linux container can have less than 60 seconds
            # of monotonic uptime.  Treat a missing entry as the first call,
            # otherwise the first @admin after every deploy is silently lost.
            if last_call is None or now - last_call >= 60:
                self._sos_calls[cooldown_key] = now
                await self._notify_administrators(bot, message, text)

        sender_chat = getattr(message, 'sender_chat', None)
        foreign_sender = sender_chat and sender_chat.id != message.chat.id
        if states['hidden_senders'] and foreign_sender:
            await self._delete(bot, message)
            await bot.send_message(message.chat.id, f'👻 Сообщение от имени канала <b>{escape(sender_chat.title)}</b> удалено настройкой PRO.')
            return True

        if states['media_filter'] and any(getattr(message, field, None) for field in MEDIA_FIELDS):
            action = await protection_action(session, chat.id, 'media_filter')
            await self._apply_content_action(bot, session, chat, message, action, 'запрещённый тип медиа')
            return True

        if states['forbidden_words']:
            normalized = text.casefold()
            words = await forbidden_words(session, chat.id)
            if next((word for word in words if word in normalized), None):
                action = await protection_action(session, chat.id, 'forbidden_words')
                await self._apply_content_action(bot, session, chat, message, action, 'запрещённое слово')
                return True
        if states['porn_filter'] and any(term in text.casefold() for term in ADULT_TERMS):
            action = await protection_action(session, chat.id, 'porn_filter')
            await self._apply_content_action(bot, session, chat, message, action, '18+ контент')
            return True
        return False

    @staticmethod
    def _message_link(message) -> str | None:
        username = getattr(message.chat, 'username', None)
        if username:
            return f'https://t.me/{username}/{message.message_id}'
        raw_chat_id = str(message.chat.id)
        if raw_chat_id.startswith('-100'):
            return f'https://t.me/c/{raw_chat_id[4:]}/{message.message_id}'
        return None

    async def _notify_administrators(self, bot, message, source_text: str) -> None:
        try:
            administrators = await bot.get_chat_administrators(message.chat.id)
        except TelegramAPIError:
            return
        targets = [member.user for member in administrators if not member.user.is_bot]
        link = self._message_link(message)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='👉 Посмотреть сообщение', url=link),
        ]]) if link else None
        caller = user_label(message.from_user) if message.from_user else 'Анонимный участник'
        excerpt = source_text.strip()
        if len(excerpt) > 350:
            excerpt = excerpt[:347] + '…'
        alert = (
            '⚠️ <b>ТРЕБУЕТСЯ АДМИНИСТРАТОР</b>\n'
            '━━━━━━━━━━━━\n\n'
            f'💬 Группа: <b>{escape(getattr(message.chat, "title", "Группа"))}</b>\n'
            f'👤 Вызвал: {caller}\n'
            f'📝 Сообщение: <i>{escape(excerpt)}</i>\n\n'
            'Пользователь вызвал администрацию через <code>@admin</code>.'
        )
        delivered = 0
        for administrator in targets:
            try:
                await bot.send_message(administrator.id, alert, reply_markup=markup)
                delivered += 1
            except TelegramAPIError:
                # Telegram allows a bot to write only to users who started it.
                continue
        try:
            if delivered:
                await bot.send_message(
                    message.chat.id,
                    f'🆘 Администраторы уведомлены в личных сообщениях: <b>{delivered}/{len(targets)}</b>.',
                    reply_to_message_id=message.message_id,
                )
            else:
                await bot.send_message(
                    message.chat.id,
                    '⚠️ Не удалось отправить вызов в личку. Администраторам нужно хотя бы один раз открыть бота и нажать /start.',
                    reply_to_message_id=message.message_id,
                )
        except TelegramAPIError:
            pass

    async def _apply_content_action(self, bot, session, chat: Chat, message, action: str, reason: str) -> None:
        deleted = await self._delete(bot, message)
        telegram_user = message.from_user
        if not telegram_user:
            return
        db_user = await upsert_user(session, telegram_user)
        title = 'СООБЩЕНИЕ УДАЛЕНО' if action == 'delete' else 'АВТОМОДЕРАЦИЯ'
        outcome = 'только удаление'
        try:
            if action == 'warn':
                session.add(Warning(chat_id=chat.id, target_user_id=db_user.id, moderator_user_id=None, reason=f'Автоматически: {reason}'))
                await session.flush()
                count = await session.scalar(select(func.count(Warning.id)).where(Warning.chat_id == chat.id, Warning.target_user_id == db_user.id, Warning.is_active.is_(True))) or 0
                session.add(ModerationAction(chat_id=chat.id, target_user_id=db_user.id, moderator_user_id=None, action=ModerationActionType.WARN, reason=f'Автоматически: {reason}'))
                outcome = f'варн · {count}/3'
                if count >= 3:
                    await bot.ban_chat_member(chat.telegram_id, telegram_user.id)
                    session.add(ModerationAction(chat_id=chat.id, target_user_id=db_user.id, moderator_user_id=None, action=ModerationActionType.BAN, reason='Автоматически: 3 предупреждения'))
                    outcome = 'бан за 3/3 варна'
            elif action == 'kick':
                await self._kick(bot, chat.telegram_id, telegram_user.id)
                outcome = 'кик с возможностью вернуться'
            elif action == 'mute':
                await bot.restrict_chat_member(chat.telegram_id, telegram_user.id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc) + timedelta(hours=1))
                session.add(ModerationAction(chat_id=chat.id, target_user_id=db_user.id, moderator_user_id=None, action=ModerationActionType.MUTE, reason=f'Автоматически: {reason}', duration_seconds=3600))
                outcome = 'мут на 1 час'
            elif action == 'ban':
                await bot.ban_chat_member(chat.telegram_id, telegram_user.id)
                session.add(ModerationAction(chat_id=chat.id, target_user_id=db_user.id, moderator_user_id=None, action=ModerationActionType.BAN, reason=f'Автоматически: {reason}'))
                outcome = 'бан'
        except TelegramAPIError:
            outcome = 'сообщение удалено, но Telegram запретил наказать этого пользователя'
        if deleted:
            try:
                await bot.send_message(chat.telegram_id, f'🛡 <b>{title}</b>\n━━━━━━━━━━━━\n\n👤 Пользователь: {user_label(db_user)}\n📝 Причина: <b>{escape(reason)}</b>\n⚖️ Действие: <b>{outcome}</b>.')
            except TelegramAPIError:
                # The moderation action has already succeeded; a missing status
                # message must not roll the database transaction back.
                pass

    @staticmethod
    def _joined(event) -> bool:
        old = event.old_chat_member
        new = event.new_chat_member
        old_present = old.status in {'member', 'administrator', 'creator'} or (old.status == 'restricted' and old.is_member)
        new_present = new.status in {'member', 'administrator', 'creator'} or (new.status == 'restricted' and new.is_member)
        return not old_present and new_present

    @staticmethod
    async def _kick(bot, chat_id: int, user_id: int) -> None:
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)

    async def _record_member_event(self, update, event, session, chat: Chat) -> None:
        joined = self._joined(event)
        old = event.old_chat_member
        new = event.new_chat_member
        old_present = old.status in {'member', 'administrator', 'creator'} or (old.status == 'restricted' and old.is_member)
        new_present = new.status in {'member', 'administrator', 'creator'} or (new.status == 'restricted' and new.is_member)
        if joined:
            event_type = 'join'
        elif old_present and not new_present:
            event_type = 'leave'
        else:
            return
        telegram_user = new.user
        if telegram_user.is_bot:
            return
        update_id = getattr(update, 'update_id', None)
        if update_id is not None and await session.scalar(select(MemberEvent.id).where(MemberEvent.telegram_update_id == update_id)):
            return
        db_user = await upsert_user(session, telegram_user)
        session.add(MemberEvent(chat_id=chat.id, user_id=db_user.id, event_type=event_type, telegram_update_id=update_id))

    async def _protect_membership(self, event, bot, chat: Chat, states: dict[str, bool]) -> bool:
        if not self._joined(event):
            old = event.old_chat_member
            new = event.new_chat_member
            old_present = old.status in {'member', 'administrator', 'creator'} or (old.status == 'restricted' and old.is_member)
            new_present = new.status in {'member', 'administrator', 'creator'} or (new.status == 'restricted' and new.is_member)
            if states['farewell'] and old_present and not new_present and not new.user.is_bot:
                try:
                    await bot.send_message(event.chat.id, f'🚪 {user_label(new.user)} покинул(а) группу. До встречи!')
                except TelegramAPIError:
                    pass
            return False
        user = event.new_chat_member.user
        if states['block_bots'] and user.is_bot:
            try:
                await bot.ban_chat_member(event.chat.id, user.id)
                await bot.send_message(event.chat.id, f'🤖 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n👤 {user_label(user)}\n📝 Причина: добавление ботов запрещено настройкой PRO.')
            except TelegramAPIError:
                pass
            return True

        if states['raid_guard'] and not user.is_bot:
            now = monotonic()
            joins = self._joins[event.chat.id]
            joins.append((user.id, now))
            while joins and now - joins[0][1] > 2:
                joins.popleft()
            if len(joins) >= 4:
                user_ids = list(dict.fromkeys(user_id for user_id, _ in joins))
                joins.clear()
                removed = 0
                for user_id in user_ids:
                    try:
                        await self._kick(bot, event.chat.id, user_id)
                        removed += 1
                    except TelegramAPIError:
                        continue
                await bot.send_message(event.chat.id, f'🛡 <b>МАССОВЫЙ ВХОД ОСТАНОВЛЕН</b>\n\nЗа 2 секунды вошли 4 участника. Удалено: <b>{removed}</b>. Они смогут вступить повторно после окончания атаки.')
                return True
        return False
