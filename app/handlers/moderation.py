from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import ChatPermissions, Message
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatModerator, ModerationAction, ModerationActionType, User, Warning
from app.filters.access import is_moderator
from app.services.parse import parse_duration
from app.services.users import find_user, upsert_chat, upsert_user
from app.services.text import user_label

router = Router(name="moderation")

ACTION_LABELS = {
    'ban': '🔨 Бан', 'unban': '🔓 Разбан',
    'mute': '🔇 Мут', 'unmute': '🔊 Размут',
    'warn': '⚠️ Варн', 'unwarn': '✅ Снятие варна',
}


def result_card(icon: str, title: str, target: User, moderator: User, *details: str) -> str:
    return (
        f'{icon} <b>{title}</b>\n'
        '━━━━━━━━━━━━\n\n'
        f'👤 Пользователь: {user_label(target)}\n'
        f'🛡 Модератор: {user_label(moderator)}'
        + (f'\n{"\n".join(details)}' if details else '')
    )


async def target_from_command(message: Message, session: AsyncSession, arguments: list[str]):
    if message.reply_to_message and message.reply_to_message.from_user:
        target = await upsert_user(session, message.reply_to_message.from_user)
        return target, arguments
    if not arguments:
        return None, []
    return await find_user(session, arguments[0]), arguments[1:]


async def allowed(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> bool:
    if not message.from_user or message.chat.type == "private" or not await is_moderator(bot, session, message.chat.id, message.from_user.id, config):
        await message.answer("Эта команда доступна создателю группы и назначенным им модераторам.")
        return False
    return True


async def target_is_protected(message: Message, bot: Bot, session: AsyncSession, target_telegram_id: int, config: Settings) -> bool:
    """Owners and peer administrators are never silently moderated by a moderator."""
    if config.is_owner(target_telegram_id):
        await message.answer("Нельзя применить действие к владельцу бота.")
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, target_telegram_id)
        if member.status in {"administrator", "creator"}:
            await message.answer("Нельзя применить действие к администратору группы.")
            return True
    except Exception:
        pass
    chat = await session.scalar(select(Chat).where(Chat.telegram_id == message.chat.id))
    target = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
    if chat and target and await session.scalar(select(ChatModerator.id).where(ChatModerator.chat_id == chat.id, ChatModerator.user_id == target.id, ChatModerator.is_active.is_(True))):
        await message.answer("Нельзя применить действие к назначенному модератору группы.")
        return True
    return False


async def log_action(session: AsyncSession, chat_id: int, target_id: int | None, moderator_id: int, action: ModerationActionType, reason: str | None = None, duration: int | None = None) -> None:
    session.add(ModerationAction(chat_id=chat_id, target_user_id=target_id, moderator_user_id=moderator_id, action=action, reason=reason, duration_seconds=duration))


async def command_context(message: Message, session: AsyncSession):
    chat = await upsert_chat(session, message.chat)
    moderator = await upsert_user(session, message.from_user)
    return chat, moderator


@router.message(Command("ban"))
async def ban(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, rest = await target_from_command(message, session, message.text.split()[1:])
    if not target: await message.answer("Пользователь не найден. Ответьте на его сообщение или укажите известный @username / ID."); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    try: await bot.ban_chat_member(message.chat.id, target.telegram_id)
    except Exception: await message.answer(f"Не удалось заблокировать {user_label(target)}. Проверьте права бота и его роль."); return
    reason = " ".join(rest) or "не указана"
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.BAN, reason)
    await message.answer(result_card('🚫', 'ПОЛЬЗОВАТЕЛЬ ЗАБАНЕН', target, moderator, f'📝 Причина: <b>{escape(reason)}</b>', '🚪 Статус: удалён из группы и не сможет вернуться до разбана.'))


@router.message(Command("unban"))
async def unban(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, _ = await target_from_command(message, session, message.text.split()[1:])
    if not target: await message.answer("Укажите известный ID или @username."); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    try: await bot.unban_chat_member(message.chat.id, target.telegram_id, only_if_banned=True)
    except Exception: await message.answer(f"Не удалось снять бан у {user_label(target)}. Проверьте права бота."); return
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.UNBAN)
    await message.answer(result_card('🔓', 'ПОЛЬЗОВАТЕЛЬ РАЗБАНЕН', target, moderator, '✅ Теперь пользователь снова может вступить в группу.'))


@router.message(Command("mute"))
async def mute(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, rest = await target_from_command(message, session, message.text.split()[1:])
    duration = parse_duration(rest[0]) if rest else None
    if not target or not duration: await message.answer("Использование: <code>/mute @user 1h причина</code> или ответьте на сообщение: <code>/mute 1h причина</code>"); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    try: await bot.restrict_chat_member(message.chat.id, target.telegram_id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc) + duration)
    except Exception: await message.answer(f"Не удалось выдать мут {user_label(target)}. Проверьте права бота."); return
    reason = " ".join(rest[1:]) or "не указана"
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.MUTE, reason, int(duration.total_seconds()))
    await message.answer(result_card('🔇', 'ПОЛЬЗОВАТЕЛЬ ЗАМЬЮЧЕН', target, moderator, f'⏱ Срок: <b>{escape(rest[0])}</b>', f'📝 Причина: <b>{escape(reason)}</b>'))


@router.message(Command("unmute"))
async def unmute(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, _ = await target_from_command(message, session, message.text.split()[1:])
    if not target: await message.answer("Укажите известный ID или @username."); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    try: await bot.restrict_chat_member(message.chat.id, target.telegram_id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False))
    except Exception: await message.answer(f"Не удалось снять мут у {user_label(target)}. Проверьте права бота."); return
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.UNMUTE)
    await message.answer(result_card('🔊', 'ПОЛЬЗОВАТЕЛЬ РАЗМЬЮЧЕН', target, moderator, '✅ Ограничение на отправку сообщений снято.'))


@router.message(Command("warn"))
async def warn(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, rest = await target_from_command(message, session, message.text.split()[1:])
    if not target: await message.answer("Пользователь не найден. Ответьте на его сообщение или укажите известный @username / ID."); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    reason = " ".join(rest) or "не указана"
    warning = Warning(chat_id=chat.id, target_user_id=target.id, moderator_user_id=moderator.id, reason=reason)
    session.add(warning)
    await session.flush()
    count = len((await session.scalars(select(Warning.id).where(Warning.chat_id == chat.id, Warning.target_user_id == target.id, Warning.is_active.is_(True)))).all())
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.WARN, reason)
    if count >= 3:
        try:
            # Three active warnings always mean a real ban: Telegram removes
            # the member and prevents rejoining until /unban is used.
            await bot.ban_chat_member(message.chat.id, target.telegram_id)
            await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.BAN, "Автоматически: 3 предупреждения")
            await message.answer(result_card('🚫', 'АВТОБАН: 3 ПРЕДУПРЕЖДЕНИЯ', target, moderator, f'⚠️ Активных варнов: <b>{count}/3</b>', f'📝 Последняя причина: <b>{escape(reason)}</b>', '🚪 Пользователь удалён из группы и забанен до ручного разбана.'))
        except Exception:
            await message.answer(result_card('⚠️', 'ТРЕТИЙ ВАРН ВЫДАН, НО АВТОБАН НЕ СРАБОТАЛ', target, moderator, f'⚠️ Активных варнов: <b>{count}/3</b>', '❌ Проверьте право бота блокировать участников.'))
        return
    await message.answer(result_card('⚠️', 'ПРЕДУПРЕЖДЕНИЕ ВЫДАНО', target, moderator, f'📝 Причина: <b>{escape(reason)}</b>', f'📊 Активные варны: <b>{count}/3</b>'))


@router.message(Command("unwarn"))
async def unwarn(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    target, _ = await target_from_command(message, session, message.text.split()[1:])
    if not target: await message.answer("Укажите известный ID или @username."); return
    if await target_is_protected(message, bot, session, target.telegram_id, config): return
    chat, moderator = await command_context(message, session)
    warning = await session.scalar(select(Warning).where(Warning.chat_id == chat.id, Warning.target_user_id == target.id, Warning.is_active.is_(True)).order_by(desc(Warning.created_at)).limit(1))
    if not warning: await message.answer(f"У пользователя {user_label(target)} нет активных предупреждений."); return
    warning.is_active = False; warning.removed_by_user_id = moderator.id; warning.removed_at = datetime.now(timezone.utc)
    await log_action(session, chat.id, target.id, moderator.id, ModerationActionType.UNWARN)
    remaining = len((await session.scalars(select(Warning.id).where(Warning.chat_id == chat.id, Warning.target_user_id == target.id, Warning.is_active.is_(True)))).all())
    await message.answer(result_card('✅', 'ПРЕДУПРЕЖДЕНИЕ СНЯТО', target, moderator, f'📊 Осталось активных варнов: <b>{remaining}/3</b>'))


@router.message(Command("history"))
async def history(message: Message, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if not await allowed(message, bot, session, config): return
    arguments = message.text.split()[1:]
    target = None
    if message.reply_to_message or arguments:
        target, _ = await target_from_command(message, session, arguments)
        if not target:
            await message.answer('Пользователь не найден. Ответьте на его сообщение либо укажите известный @username / ID.')
            return
    chat, _ = await command_context(message, session)
    query = select(ModerationAction).where(ModerationAction.chat_id == chat.id)
    if target:
        query = query.where(ModerationAction.target_user_id == target.id)
    actions = (await session.scalars(query.order_by(desc(ModerationAction.created_at), desc(ModerationAction.id)).limit(10))).all()

    entries = []
    for action in actions:
        action_target = await session.get(User, action.target_user_id) if action.target_user_id else None
        action_moderator = await session.get(User, action.moderator_user_id) if action.moderator_user_id else None
        action_key = action.action.value if hasattr(action.action, 'value') else str(action.action)
        created_at = action.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        entries.append(
            f'<b>{ACTION_LABELS.get(action_key, escape(action_key))}</b> · <code>{created_at.astimezone().strftime("%d.%m.%Y %H:%M")}</code>\n'
            f'👤 {user_label(action_target) if action_target else "пользователь удалён"}\n'
            f'🛡 {user_label(action_moderator) if action_moderator else "автоматика бота"}\n'
            f'📝 {escape(action.reason or "без причины")}'
        )
    scope = f'Пользователь: {user_label(target)}' if target else f'Группа: <b>{escape(chat.title)}</b>'
    text = (
        f'📋 <b>ИСТОРИЯ МОДЕРАЦИИ</b>\n━━━━━━━━━━━━\n\n{scope}\n\n'
        + ('\n\n'.join(entries) if entries else '<i>Действий пока нет.</i>')
        + '\n\n<i>Показаны последние 10 действий. Для истории одного участника ответьте на его сообщение командой /history.</i>'
    )
    await message.answer(text)
