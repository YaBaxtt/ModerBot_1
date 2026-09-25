from __future__ import annotations

import asyncio
import logging
from html import escape
from random import shuffle
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatMemberUpdated, ChatPermissions, EphemeralMessageParameters, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import Chat, JoinVerification, User
from app.services.text import display_name, user_label
from app.services.users import upsert_chat, upsert_user
from app.services.verification import random_question
from app.keyboards.common import back_button
from app.services.features import feature_enabled
from app.services.protections import protection_states

router = Router(name="verification")
log = logging.getLogger(__name__)

OPEN_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_change_info=False, can_invite_users=True,
    can_pin_messages=False,
)


def question_keyboard(challenge_id: int, question_key: str, options: tuple[str, ...]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=option,
            callback_data=f"verify:{challenge_id}:{question_key}:{index}",
        )
        for index, option in enumerate(options)
    ]
    shuffle(buttons)
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


async def kick_unverified(bot: Bot, *, chat_telegram_id: int, user_telegram_id: int) -> bool:
    """Kick rather than permanently ban: ban + immediate unban permits rejoining."""
    try:
        await bot.ban_chat_member(chat_telegram_id, user_telegram_id)
        await bot.unban_chat_member(chat_telegram_id, user_telegram_id, only_if_banned=True)
        return True
    except Exception:
        log.warning("Could not remove unverified member", exc_info=True)
        return False


async def announce_verification_kick(bot: Bot, chat: Chat, user: User) -> None:
    """Make an automatic removal understandable without pinging the member."""
    try:
        await bot.send_message(
            chat.telegram_id,
            '🚪 <b>УЧАСТНИК УДАЛЁН</b>\n'
            '━━━━━━━━━━━━\n\n'
            f'👤 Пользователь: {user_label(user)}\n'
            '📝 Причина: <b>не прошёл проверку при входе вовремя</b>.\n'
            '🔁 Это кик, а не вечный бан — пользователь сможет вступить снова.',
        )
    except Exception:
        log.warning('Could not announce verification kick for user %s', user.telegram_id, exc_info=True)


async def expire_after_delay(bot: Bot, session_factory: async_sessionmaker[AsyncSession], challenge_id: int, seconds: float) -> None:
    await asyncio.sleep(max(seconds, 0))
    async with session_factory() as session:
        row = await session.get(JoinVerification, challenge_id)
        if not row or row.is_verified or row.completed_at or row.expires_at.replace(tzinfo=None) > datetime.utcnow():
            return
        if not await feature_enabled(session, 'verification'):
            if await release_challenge(bot, session, row):
                await session.commit()
            return
        chat = await session.get(Chat, row.chat_id)
        user = await session.get(User, row.user_id)
        if chat and user:
            member = await bot.get_chat_member(chat.telegram_id, user.telegram_id)
            if member.status in {"left", "kicked", "administrator", "creator"}:
                row.completed_at = datetime.utcnow()
                await session.commit()
            elif await kick_unverified(bot, chat_telegram_id=chat.telegram_id, user_telegram_id=user.telegram_id):
                await announce_verification_kick(bot, chat, user)
                row.completed_at = datetime.utcnow()
                await session.commit()


async def recover_pending_verifications(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Resume timeout enforcement after a bot restart."""
    async with session_factory() as session:
        challenges = (await session.scalars(select(JoinVerification).where(JoinVerification.is_verified.is_(False), JoinVerification.completed_at.is_(None)))).all()
        if not await feature_enabled(session, 'verification'):
            for challenge in challenges:
                await release_challenge(bot, session, challenge)
            await session.commit()
            return
        now = datetime.utcnow()
        for challenge in challenges:
            expiry = challenge.expires_at.replace(tzinfo=None)
            asyncio.create_task(expire_after_delay(bot, session_factory, challenge.id, (expiry - now).total_seconds()))


def member_is_present(member) -> bool:
    if member.status == "restricted":
        return member.is_member
    return member.status in {"member", "administrator", "creator"}


async def release_challenge(bot: Bot, session: AsyncSession, challenge: JoinVerification) -> bool:
    chat = await session.get(Chat, challenge.chat_id)
    user = await session.get(User, challenge.user_id)
    if not chat or not user:
        challenge.completed_at = datetime.utcnow()
        return True
    try:
        member = await bot.get_chat_member(chat.telegram_id, user.telegram_id)
        if member.status not in {'left', 'kicked', 'administrator', 'creator'}:
            info = await bot.get_chat(chat.telegram_id)
            await bot.restrict_chat_member(chat.telegram_id, user.telegram_id, permissions=info.permissions or OPEN_PERMISSIONS)
        challenge.completed_at = datetime.utcnow()
        return True
    except Exception:
        log.warning('Could not release pending verification %s', challenge.id, exc_info=True)
        return False


async def release_pending_verifications(bot: Bot, session: AsyncSession) -> tuple[int, int]:
    rows = (await session.scalars(select(JoinVerification).where(JoinVerification.is_verified.is_(False), JoinVerification.completed_at.is_(None)))).all()
    released = failed = 0
    for row in rows:
        if await release_challenge(bot, session, row): released += 1
        else: failed += 1
    await session.commit()
    return released, failed


@router.chat_member()
async def new_member(event: ChatMemberUpdated, bot: Bot, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], config: Settings) -> None:
    telegram_user = event.new_chat_member.user
    # Telegram can report several status combinations after an unban or an
    # invite-link rejoin. Membership transition is the reliable signal.
    was_member = member_is_present(event.old_chat_member)
    is_member = member_is_present(event.new_chat_member)
    log.info("Chat-member update: user=%s was_member=%s is_member=%s", telegram_user.id, was_member, is_member)
    if telegram_user.is_bot or config.is_owner(telegram_user.id) or was_member or not is_member or event.new_chat_member.status in {"administrator", "creator"}:
        return
    if not await feature_enabled(session, 'verification'):
        return
    user = await upsert_user(session, telegram_user)
    chat = await upsert_chat(session, event.chat)
    chat_settings = await protection_states(session, chat.id)
    if not chat_settings['captcha']:
        if chat_settings['welcome']:
            try:
                await bot.send_message(user.telegram_id, f'👋 <b>Добро пожаловать в {escape(chat.title)}!</b>\n\nПроверка при входе для этой группы отключена. Приятного общения!')
            except TelegramAPIError:
                pass
        return
    try:
        await bot.restrict_chat_member(event.chat.id, telegram_user.id, permissions=ChatPermissions(can_send_messages=False))
    except Exception:
        log.warning("Could not restrict new member for verification", exc_info=True)
        try:
            await bot.send_message(event.chat.id, "⚠️ Не удалось запустить проверку нового участника: проверьте право бота ограничивать пользователей.")
        except Exception:
            log.warning("Could not notify chat about missing verification permission", exc_info=True)
        return

    question = await random_question(session)
    expires_at = datetime.utcnow() + timedelta(seconds=config.join_verification_timeout_seconds)
    challenge = await session.scalar(select(JoinVerification).where(JoinVerification.chat_id == chat.id, JoinVerification.user_id == user.id))
    if challenge:
        challenge.question_key = question.key
        challenge.correct_index = question.correct_index
        challenge.expires_at = expires_at
        challenge.is_verified = False
        challenge.completed_at = None
    else:
        challenge = JoinVerification(chat_id=chat.id, user_id=user.id, question_key=question.key, correct_index=question.correct_index, expires_at=expires_at)
        session.add(challenge)
        await session.flush()

    text = f"👋 <b>{display_name(telegram_user)}</b>, пройдите короткую проверку, чтобы получить доступ к сообщениям.\n\n⏱ У вас <b>{config.join_verification_timeout_seconds} секунд</b>. Если не пройти проверку, бот удалит вас из чата.\n\n<b>Вопрос:</b> {escape(question.text)}"
    await bot.send_message(event.chat.id, text, reply_markup=question_keyboard(challenge.id, question.key, question.options))
    await session.commit()
    asyncio.create_task(expire_after_delay(bot, session_factory, challenge.id, config.join_verification_timeout_seconds + 1))


@router.callback_query(F.data.startswith("verify:"))
async def answer_verification(callback: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    try:
        _, raw_id, question_key, raw_choice = callback.data.split(":")
        challenge_id, selected = int(raw_id), int(raw_choice)
    except (ValueError, AttributeError):
        await callback.answer("Некорректный ответ", show_alert=True)
        return
    challenge = await session.get(JoinVerification, challenge_id, with_for_update=True)
    if not challenge or challenge.user_id is None:
        await callback.answer("Проверка не найдена", show_alert=True)
        return
    user = await session.get(User, challenge.user_id)
    if not user or user.telegram_id != callback.from_user.id:
        await callback.answer("Эта проверка предназначена не вам.", show_alert=True)
        return
    if challenge.is_verified:
        await callback.answer("Проверка уже пройдена.", show_alert=True)
        return
    if challenge.completed_at:
        await callback.answer("Проверка уже завершена.", show_alert=True)
        return
    if challenge.expires_at.replace(tzinfo=None) <= datetime.utcnow():
        await callback.answer("Время вышло.", show_alert=True)
        chat = await session.get(Chat, challenge.chat_id)
        if chat:
            if await kick_unverified(bot, chat_telegram_id=chat.telegram_id, user_telegram_id=user.telegram_id):
                await announce_verification_kick(bot, chat, user)
                challenge.completed_at = datetime.utcnow()
                await session.commit()
        return
    if question_key != challenge.question_key or challenge.correct_index is None or selected != challenge.correct_index:
        await callback.answer("Неверно. Попробуйте ещё раз.", show_alert=True)
        return
    chat = await session.get(Chat, challenge.chat_id)
    if not chat:
        await callback.answer("Чат не найден", show_alert=True)
        return
    try:
        info = await bot.get_chat(chat.telegram_id)
        await bot.restrict_chat_member(chat.telegram_id, user.telegram_id, permissions=info.permissions or OPEN_PERMISSIONS)
    except Exception:
        log.warning("Could not lift verification restriction", exc_info=True)
        await callback.answer("Не удалось выдать доступ. Обратитесь к администратору.", show_alert=True)
        return
    challenge.is_verified = True
    challenge.completed_at = datetime.utcnow()
    await session.commit()
    await callback.answer("Проверка пройдена!")
    settings = await protection_states(session, chat.id)
    await send_private_welcome(callback, bot, chat, send_welcome=settings['welcome'])


async def send_private_welcome(callback, bot, chat, *, send_welcome: bool = True):
    text = f"✅ <b>Проверка пройдена!</b>\n\n<b>Добро пожаловать, {display_name(callback.from_user)}!</b>\nВы вошли в <b>{escape(chat.title)}</b>. Теперь можно общаться.\n\n🛡 Я бот-модератор: защищаю чат от спама, принимаю жалобы и считаю активность.\n/me — профиль, /top — рейтинг, /rules — правила. В личке доступны магазин и /report @username для жалобы.\n\nПриятного общения!"
    if send_welcome:
        try:
            await bot.send_message(chat.telegram_id, text, ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=callback.from_user.id, callback_query_id=callback.id))
        except TelegramAPIError:
            # Never fall back to a public welcome when privacy was requested.
            try:
                await bot.send_message(callback.from_user.id, text, reply_markup=back_button())
            except TelegramAPIError:
                log.info('Private welcome unavailable for verified member')
    try:
        await callback.message.delete()
    except TelegramAPIError:
        log.info('Could not delete completed verification prompt')
