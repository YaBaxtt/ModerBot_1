from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatProtectionSetting


@dataclass(frozen=True)
class Protection:
    key: str
    title: str
    description: str
    premium: bool = True
    default_enabled: bool = False


GROUP_CONTROLS = (
    Protection('antispam', '🛡 Антиспам', 'Удаляет слишком частые и одинаковые сообщения.', premium=False, default_enabled=True),
    Protection('captcha', '🧠 Капча', 'Проверяет новых участников перед доступом к сообщениям.', premium=False, default_enabled=True),
    Protection('welcome', '👋 Приветствие', 'Отправляет приветствие после успешной проверки.', premium=False, default_enabled=True),
    Protection('farewell', '🚪 Прощание', 'Сообщает, когда участник покидает группу.', premium=False),
    Protection('sos_admin', '🆘 SOS @Admin', 'Вызывает администраторов группы по сообщению @admin.', premium=False, default_enabled=True),
    Protection('media_filter', '🎞 Медиа-фильтр', 'Удаляет фото, видео, GIF, стикеры, файлы и другое медиа.'),
    Protection('forbidden_words', '🔤 Запрещённые слова', 'Удаляет сообщения со словами из вашего списка.'),
    Protection('porn_filter', '🔞 Фильтр 18+', 'Находит распространённые 18+ слова и ссылки в тексте.'),
    Protection('raid_guard', '🛡 Массовый вход', 'Кикает волну из 4 участников, вошедших за 2 секунды.'),
    Protection('block_bots', '🤖 Блокировка ботов', 'Банит добавляемых в группу ботов.'),
    Protection('hidden_senders', '👻 Скрытые отправители', 'Удаляет сообщения, отправленные от имени чужих каналов.'),
)
PROTECTIONS = tuple(item for item in GROUP_CONTROLS if item.premium)
PROTECTION_BY_KEY = {item.key: item for item in GROUP_CONTROLS}
MODERATION_ACTIONS = {'delete', 'warn', 'kick', 'mute', 'ban'}


async def protection_rows(session: AsyncSession, chat_id: int) -> dict[str, ChatProtectionSetting]:
    rows = (await session.scalars(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat_id))).all()
    return {row.key: row for row in rows}


async def protection_states(session: AsyncSession, chat_id: int) -> dict[str, bool]:
    rows = await protection_rows(session, chat_id)
    return {item.key: bool(rows[item.key].enabled) if item.key in rows else item.default_enabled for item in GROUP_CONTROLS}


async def set_protection(session: AsyncSession, chat_id: int, key: str, enabled: bool) -> ChatProtectionSetting:
    if key not in PROTECTION_BY_KEY:
        raise KeyError(key)
    row = await session.scalar(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat_id, ChatProtectionSetting.key == key))
    if row:
        row.enabled = enabled
    else:
        row = ChatProtectionSetting(chat_id=chat_id, key=key, enabled=enabled)
        session.add(row)
    return row


async def forbidden_words(session: AsyncSession, chat_id: int) -> list[str]:
    row = await session.scalar(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat_id, ChatProtectionSetting.key == 'forbidden_words'))
    if not row or not row.value:
        return []
    try:
        saved = json.loads(row.value)
    except (TypeError, ValueError):
        return []
    values = saved.get('words', []) if isinstance(saved, dict) else saved
    return [str(value).casefold().strip() for value in values if str(value).strip()]


async def set_forbidden_words(session: AsyncSession, chat_id: int, words: list[str]) -> ChatProtectionSetting:
    row = await set_protection(session, chat_id, 'forbidden_words', bool(words))
    action = await protection_action(session, chat_id, 'forbidden_words')
    row.value = json.dumps({'words': words, 'action': action}, ensure_ascii=False)
    return row


async def protection_action(session: AsyncSession, chat_id: int, key: str) -> str:
    defaults = {'forbidden_words': 'ban', 'porn_filter': 'ban', 'media_filter': 'delete'}
    row = await session.scalar(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat_id, ChatProtectionSetting.key == key))
    if row and row.value:
        try:
            saved = json.loads(row.value)
            action = saved.get('action') if isinstance(saved, dict) else None
            if action in MODERATION_ACTIONS:
                return action
        except (TypeError, ValueError):
            pass
    return defaults.get(key, 'delete')


async def set_protection_action(session: AsyncSession, chat_id: int, key: str, action: str) -> ChatProtectionSetting:
    if action not in MODERATION_ACTIONS or key not in {'forbidden_words', 'porn_filter', 'media_filter'}:
        raise KeyError((key, action))
    row = await set_protection(session, chat_id, key, (await protection_states(session, chat_id))[key])
    saved: dict = {}
    if row.value:
        try:
            current = json.loads(row.value)
            if isinstance(current, dict):
                saved.update(current)
            elif key == 'forbidden_words' and isinstance(current, list):
                saved['words'] = current
        except (TypeError, ValueError):
            pass
    saved['action'] = action
    row.value = json.dumps(saved, ensure_ascii=False)
    return row
