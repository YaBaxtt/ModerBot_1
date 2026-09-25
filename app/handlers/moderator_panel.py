from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging
import re

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatModerator, MemberProfileSnapshot, ModerationAction, User, UserChatStats
from app.keyboards.common import back_button
from app.services.moderators import active_assignment, can_manage_chat, chat_accesses, revoke_moderator, set_moderator, telegram_role
from app.services.text import user_label
from app.services.users import find_user, upsert_chat, upsert_user


router = Router(name='moderator_panel')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
log = logging.getLogger(__name__)


class AddModeratorForm(StatesGroup):
    target = State()


ACTION_NAMES = {
    'ban': '🔨 Бан', 'unban': '🔓 Снятие бана',
    'mute': '🔇 Мут', 'unmute': '🔊 Снятие мута',
    'warn': '⚠️ Варн', 'unwarn': '✅ Снятие варна',
}


def local_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime('%d.%m.%Y %H:%M')


@router.my_chat_member()
async def remember_bot_chat(event: ChatMemberUpdated, session: AsyncSession) -> None:
    """Register a group immediately when the bot is added, before messages arrive."""
    if event.chat.type not in {'group', 'supergroup'}:
        return
    status = event.new_chat_member.status
    if status in {'left', 'kicked'}:
        chat = await session.scalar(select(Chat).where(Chat.telegram_id == event.chat.id))
        if chat:
            chat.is_active = False
        return
    await upsert_chat(session, event.chat)


async def authorize_chat(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings, chat_id: int, *, manage: bool = False) -> Chat | None:
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.is_active:
        await callback.answer('Группа недоступна', show_alert=True)
        return None
    allowed = await can_manage_chat(bot, chat, callback.from_user.id, config) if manage else (
        await can_manage_chat(bot, chat, callback.from_user.id, config)
        or bool(await active_assignment(session, chat.id, callback.from_user.id))
    )
    if not allowed:
        await callback.answer('У вас нет доступа к этой группе.', show_alert=True)
        return None
    if not manage and not await can_manage_chat(bot, chat, callback.from_user.id, config):
        role = await telegram_role(bot, chat.telegram_id, callback.from_user.id)
        if role in {None, 'left', 'kicked'}:
            await callback.answer('Вы больше не состоите в этой группе.', show_alert=True)
            return None
    return chat


def home_markup(accesses: list[tuple[Chat, bool]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f'{"👑" if managed else "🛡"} {chat.title[:55]}',
        callback_data=f'moder:chat:{chat.id}',
        style=None,
    )] for chat, managed in accesses]
    rows.append([InlineKeyboardButton(text='⬅️ В меню', callback_data='nav:private_main')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_home(target: Message, session: AsyncSession, bot: Bot, config: Settings, user_id: int, *, edit: bool) -> None:
    accesses = await chat_accesses(session, bot, user_id, config)
    if accesses:
        text = '🛡 <b>МОДЕРАТОРСКАЯ</b>\n━━━━━━━━━━━━\n\nВы видите только группы, которыми владеете или где вас назначили доверенным модератором.\n\n<blockquote>👑 — можете назначать модераторов\n🛡 — можете использовать команды модерации</blockquote>'
    else:
        text = '🛡 <b>МОДЕРАТОРСКАЯ</b>\n━━━━━━━━━━━━\n\nУ вас пока нет доступных групп.\n\n<blockquote>Раздел появится, если вы создатель подключённой группы или её владелец назначит вас доверенным модератором.</blockquote>'
    if edit and target.text:
        await target.edit_text(text, reply_markup=home_markup(accesses))
    else:
        await target.answer(text, reply_markup=home_markup(accesses))


@router.message(Command('moder'))
async def moderator_command(message: Message, session: AsyncSession, bot: Bot, config: Settings, state: FSMContext) -> None:
    await state.clear()
    await upsert_user(session, message.from_user)
    await render_home(message, session, bot, config, message.from_user.id, edit=False)


@router.callback_query(F.data == 'moder:home')
async def moderator_home(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings, state: FSMContext) -> None:
    await state.clear()
    await upsert_user(session, callback.from_user)
    await callback.answer()
    await render_home(callback.message, session, bot, config, callback.from_user.id, edit=True)


async def render_chat(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings, chat: Chat) -> None:
    managed = await can_manage_chat(bot, chat, callback.from_user.id, config)
    assignments = (await session.scalars(select(ChatModerator).where(ChatModerator.chat_id == chat.id, ChatModerator.is_active.is_(True)).order_by(ChatModerator.granted_at))).all()
    text = f'🛡 <b>{escape(chat.title)}</b>\n━━━━━━━━━━━━\n\nВаш уровень: <b>{"владелец группы" if managed else "доверенный модератор"}</b>\nНазначено модераторов: <b>{len(assignments)}</b>\n\n<blockquote>Доступны команды /warn, /unwarn, /mute, /unmute, /ban, /unban и /history.</blockquote>'
    rows = []
    for assignment in assignments:
        user = await session.get(User, assignment.user_id)
        if user and (managed or user.telegram_id == callback.from_user.id):
            rows.append([InlineKeyboardButton(text=f'👤 {user.first_name[:45]}', callback_data=f'moder:view:{chat.id}:{user.id}')])
    if managed:
        rows.append([InlineKeyboardButton(text='➕ Добавить модератора', callback_data=f'moder:add_start:{chat.id}', style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text='⬅️ К группам', callback_data='moder:home')])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('moder:chat:'))
async def moderator_chat(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings, state: FSMContext) -> None:
    await state.clear()
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except ValueError:
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await authorize_chat(callback, session, bot, config, chat_id)
    if not chat:
        return
    await callback.answer()
    await render_chat(callback, session, bot, config, chat)


async def known_candidates(session: AsyncSession, chat_id: int) -> list[User]:
    snapshot_users = (await session.scalars(
        select(User)
        .join(MemberProfileSnapshot, MemberProfileSnapshot.user_id == User.id)
        .where(MemberProfileSnapshot.chat_id == chat_id)
        .order_by(MemberProfileSnapshot.updated_at.desc())
        .limit(20)
    )).all()
    active_users = (await session.scalars(
        select(User)
        .join(UserChatStats, UserChatStats.user_id == User.id)
        .where(UserChatStats.chat_id == chat_id)
        .order_by(UserChatStats.last_message_at.desc())
        .limit(20)
    )).all()
    result = []
    seen = set()
    for user in (*snapshot_users, *active_users):
        if user.id not in seen:
            result.append(user)
            seen.add(user.id)
        if len(result) == 20:
            break
    return result


@router.callback_query(F.data.startswith('moder:add_start:'))
async def add_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    chat_id = int(callback.data.rsplit(':', 1)[1])
    chat = await authorize_chat(callback, session, bot, config, chat_id, manage=True)
    if not chat:
        return
    await state.clear()
    await state.set_state(AddModeratorForm.target)
    await state.update_data(moderator_chat_id=chat.id)
    candidates = await known_candidates(session, chat.id)
    active_ids = set((await session.scalars(select(ChatModerator.user_id).where(ChatModerator.chat_id == chat.id, ChatModerator.is_active.is_(True)))).all())
    rows = [[InlineKeyboardButton(text=f'{user.first_name[:40]}{f" · @{user.username}" if user.username else ""}', callback_data=f'moder:add:{chat.id}:{user.id}')] for user in candidates if user.telegram_id != callback.from_user.id and user.id not in active_ids]
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=f'moder:chat:{chat.id}')])
    await callback.answer()
    await callback.message.edit_text(f'➕ <b>ДОБАВЛЕНИЕ МОДЕРАТОРА</b>\n━━━━━━━━━━━━\n\nГруппа: <b>{escape(chat.title)}</b>\n\nОтправьте <code>@username</code>, Telegram ID, ссылку <code>t.me/username</code> или перешлите сообщение пользователя. Также можно выбрать известного участника ниже.\n\n<i>Telegram не предоставляет ботам полный список подписчиков — здесь показываются последние известные участники.</i>', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def forwarded_user(message: Message):
    origin = getattr(message, 'forward_origin', None)
    return getattr(origin, 'sender_user', None)


async def resolve_candidate(message: Message, session: AsyncSession) -> User | None:
    forwarded = forwarded_user(message)
    if forwarded:
        return await upsert_user(session, forwarded)
    text = (message.text or '').strip()
    match = re.fullmatch(r'(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})/?', text, re.IGNORECASE)
    if match:
        text = '@' + match.group(1)
    return await find_user(session, text) if text else None


async def grant_candidate(event: CallbackQuery | Message, session: AsyncSession, bot: Bot, config: Settings, chat_id: int, user: User, state: FSMContext) -> None:
    source_user = event.from_user
    chat = await session.get(Chat, chat_id)
    if not chat or not await can_manage_chat(bot, chat, source_user.id, config):
        if isinstance(event, CallbackQuery):
            await event.answer('Нет доступа', show_alert=True)
        else:
            await event.answer('У вас больше нет доступа к управлению этой группой.', reply_markup=back_button('moder:home'))
        return
    if user.telegram_id == source_user.id:
        text = 'Создателя группы не нужно назначать модератором — у него уже есть полный доступ.'
        if isinstance(event, CallbackQuery): await event.answer(text, show_alert=True)
        else: await event.answer(text, reply_markup=back_button(f'moder:chat:{chat.id}'))
        return
    role = await telegram_role(bot, chat.telegram_id, user.telegram_id)
    if role in {None, 'left', 'kicked'}:
        text = 'Пользователь не состоит в этой группе или Telegram не дал проверить его статус.'
        if isinstance(event, CallbackQuery): await event.answer(text, show_alert=True)
        else: await event.answer(text, reply_markup=back_button(f'moder:chat:{chat.id}'))
        return
    grantor = await upsert_user(session, source_user)
    await set_moderator(session, chat_id=chat.id, user_id=user.id, granted_by_user_id=grantor.id)
    await session.commit()
    await state.clear()
    if isinstance(event, CallbackQuery):
        await event.answer('Модератор добавлен')
        await render_chat(event, session, bot, config, chat)
    else:
        await event.answer(f'✅ {user_label(user)} назначен модератором группы <b>{escape(chat.title)}</b>.', reply_markup=back_button(f'moder:chat:{chat.id}'))
    try:
        await bot.send_message(
            user.telegram_id,
            f'🛡 Вас назначили доверенным модератором группы <b>{escape(chat.title)}</b>.\n\nТеперь вам доступны защита, правила, статистика и другие настройки этой группы. Назначать или удалять модераторов по-прежнему может только создатель группы.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='⚙️ Настройки группы', callback_data=f'menu:group_settings:{chat.id}', style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text='🛡 Модераторская', callback_data=f'moder:chat:{chat.id}')],
            ]),
        )
    except TelegramAPIError:
        log.info('Could not notify moderator %s about assignment', user.telegram_id)


@router.callback_query(F.data.startswith('moder:add:'))
async def add_known(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, raw_chat_id, raw_user_id = callback.data.split(':')
        chat_id, user_id = int(raw_chat_id), int(raw_user_id)
    except (ValueError, TypeError):
        await callback.answer('Некорректные данные', show_alert=True)
        return
    user = await session.get(User, user_id)
    if not user:
        await callback.answer('Пользователь не найден', show_alert=True)
        return
    await grant_candidate(callback, session, bot, config, chat_id, user, state)


@router.message(AddModeratorForm.target)
async def add_from_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data()
    user = await resolve_candidate(message, session)
    if not user:
        await message.answer('Пользователь пока неизвестен боту. Попросите его написать в группе или перешлите его сообщение без скрытого автора.', reply_markup=back_button(f'moder:chat:{data["moderator_chat_id"]}'))
        return
    await grant_candidate(message, session, bot, config, int(data['moderator_chat_id']), user, state)


@router.callback_query(F.data.startswith('moder:view:'))
async def moderator_detail(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, raw_chat_id, raw_user_id = callback.data.split(':')
        chat_id, user_id = int(raw_chat_id), int(raw_user_id)
    except (ValueError, TypeError):
        await callback.answer('Некорректные данные', show_alert=True)
        return
    chat = await authorize_chat(callback, session, bot, config, chat_id)
    assignment = await session.scalar(select(ChatModerator).where(ChatModerator.chat_id == chat_id, ChatModerator.user_id == user_id, ChatModerator.is_active.is_(True))) if chat else None
    user = await session.get(User, user_id) if assignment else None
    if not chat or not assignment or not user:
        await callback.answer('Назначение не найдено', show_alert=True)
        return
    managed = await can_manage_chat(bot, chat, callback.from_user.id, config)
    if not managed and callback.from_user.id != user.telegram_id:
        await callback.answer('Нет доступа к статистике другого модератора', show_alert=True)
        return
    grantor = await session.get(User, assignment.granted_by_user_id) if assignment.granted_by_user_id else None
    counts = dict((await session.execute(select(ModerationAction.action, func.count(ModerationAction.id)).where(ModerationAction.chat_id == chat.id, ModerationAction.moderator_user_id == user.id).group_by(ModerationAction.action))).all())
    actions = (await session.scalars(select(ModerationAction).where(ModerationAction.chat_id == chat.id, ModerationAction.moderator_user_id == user.id).order_by(ModerationAction.created_at.desc()).limit(5))).all()
    lines = []
    for action in actions:
        target = await session.get(User, action.target_user_id) if action.target_user_id else None
        lines.append(f'• {ACTION_NAMES.get(str(action.action), str(action.action))} → {user_label(target) if target else "пользователь удалён"}\n  <i>{local_datetime(action.created_at)} · {escape(action.reason or "без причины")}</i>')
    total = sum(counts.values())
    text = f'👤 <b>КАРТОЧКА МОДЕРАТОРА</b>\n━━━━━━━━━━━━\n\n{user_label(user)}\nГруппа: <b>{escape(chat.title)}</b>\nНазначен: <b>{local_datetime(assignment.granted_at)}</b>\nКем: {user_label(grantor) if grantor else "неизвестно"}\n\n<b>СТАТИСТИКА</b>\nВсего действий: <b>{total}</b>\n🔨 Баны: <b>{counts.get("ban", 0)}</b> · 🔇 Муты: <b>{counts.get("mute", 0)}</b> · ⚠️ Варны: <b>{counts.get("warn", 0)}</b>\n\n<b>ПОСЛЕДНИЕ 5 ДЕЙСТВИЙ</b>\n' + ('\n'.join(lines) if lines else '<i>Действий пока нет.</i>')
    rows = []
    if managed:
        rows.append([InlineKeyboardButton(text='🗑 Удалить модератора', callback_data=f'moder:remove:{chat.id}:{user.id}')])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=f'moder:chat:{chat.id}')])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('moder:remove:'))
async def remove_moderator(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, raw_chat_id, raw_user_id = callback.data.split(':')
        chat_id, user_id = int(raw_chat_id), int(raw_user_id)
    except (ValueError, TypeError):
        await callback.answer('Некорректные данные', show_alert=True)
        return
    chat = await authorize_chat(callback, session, bot, config, chat_id, manage=True)
    if not chat:
        return
    user = await session.get(User, user_id)
    removed = await revoke_moderator(session, chat_id=chat.id, user_id=user_id)
    await session.commit()
    await callback.answer('Доступ отозван' if removed else 'Модератор уже удалён')
    if removed and user:
        await callback.message.edit_text(
            '🗑 <b>МОДЕРАТОР РАЗЖАЛОВАН</b>\n'
            '━━━━━━━━━━━━\n\n'
            f'👤 Пользователь: {user_label(user)}\n'
            f'💬 Группа: <b>{escape(chat.title)}</b>\n'
            '✅ Доступ к командам модерации этой группы отозван.',
            reply_markup=back_button(f'moder:chat:{chat.id}'),
        )
    else:
        await render_chat(callback, session, bot, config, chat)
    if removed and user:
        try:
            await bot.send_message(user.telegram_id, f'ℹ️ Ваш доступ модератора группы <b>{escape(chat.title)}</b> отозван.', reply_markup=back_button('moder:home'))
        except TelegramAPIError:
            log.info('Could not notify moderator %s about revocation', user.telegram_id)
