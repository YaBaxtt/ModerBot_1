from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatProtectionSetting, DailyActivity, JoinVerification, MemberEvent, ModerationAction, ModerationActionType, Warning
from app.keyboards.common import back_button
from app.services.moderators import can_moderate_chat
from app.services.premium import chat_has_pro
from app.services.protections import PROTECTION_BY_KEY, forbidden_words, protection_action, protection_states, set_forbidden_words, set_protection, set_protection_action


router = Router(name='group_controls')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')


class ForbiddenWordsForm(StatesGroup):
    words = State()


class GroupRulesForm(StatesGroup):
    text = State()


async def managed_chat(session: AsyncSession, bot: Bot, config: Settings, chat_id: int, user_id: int) -> Chat | None:
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.is_active or not await can_moderate_chat(bot, session, chat, user_id, config):
        return None
    return chat


async def render_protection(message: Message, session: AsyncSession, chat: Chat, key: str, *, note: str | None = None) -> None:
    item = PROTECTION_BY_KEY[key]
    states = await protection_states(session, chat.id)
    enabled = states[key]
    details = ''
    if key == 'forbidden_words':
        words = await forbidden_words(session, chat.id)
        details = '\n\n<b>Список:</b> ' + (', '.join(escape(word) for word in words[:30]) if words else '<i>пока пуст</i>')
    action = await protection_action(session, chat.id, key) if key in {'forbidden_words', 'porn_filter', 'media_filter'} else None
    if action:
        action_names = {'delete': 'удалить', 'warn': 'варн', 'kick': 'кик', 'mute': 'мут на 1 час', 'ban': 'бан'}
        details += f'\n<b>Наказание:</b> {action_names[action]}'
    text = f'{item.title}\n━━━━━━━━━━━━\n\n💬 Группа: <b>{escape(chat.title)}</b>\n\n{item.description}\n\nСтатус: {"✅ <b>включено</b>" if enabled else "❌ <b>выключено</b>"}{details}'
    if note:
        text += f'\n\n{note}'
    rows = [[
        InlineKeyboardButton(text='✅ Включить', callback_data=f'groupcfg:toggle:{key}:{chat.id}:1', style=ButtonStyle.SUCCESS if enabled else None),
        InlineKeyboardButton(text='❌ Выключить', callback_data=f'groupcfg:toggle:{key}:{chat.id}:0', style=ButtonStyle.SUCCESS if not enabled else None),
    ]]
    if action:
        action_names = [('delete', '🗑 Удалить'), ('warn', '⚠️ Варн'), ('kick', '🚪 Кик'), ('mute', '🔇 Мут 1ч'), ('ban', '🚫 Бан')]
        buttons = [InlineKeyboardButton(text=label, callback_data=f'groupcfg:action:{key}:{chat.id}:{value}', style=ButtonStyle.SUCCESS if value == action else None) for value, label in action_names]
        rows.extend([buttons[:3], buttons[3:]])
    if key == 'forbidden_words':
        rows.append([InlineKeyboardButton(text='✏️ Изменить список слов', callback_data=f'groupcfg:words:{chat.id}')])
    rows.append([InlineKeyboardButton(text='⬅️ К настройкам', callback_data=f'menu:group_settings:{chat.id}')])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('groupcfg:view:'))
async def protection_view(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, key, raw_chat_id = callback.data.split(':')
        chat_id = int(raw_chat_id)
    except (AttributeError, TypeError, ValueError):
        await callback.answer('Некорректные настройки', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat or key not in PROTECTION_BY_KEY:
        await callback.answer('Нет доступа к настройкам группы.', show_alert=True)
        return
    if PROTECTION_BY_KEY[key].premium and not await chat_has_pro(session, chat.id, callback.from_user.id, config):
        await callback.answer('Эта функция доступна в PRO.', show_alert=True)
        await callback.message.edit_text('🔒 <b>НУЖЕН PRO-ДОСТУП</b>\n\nКупите все расширенные функции один раз и используйте их в этой группе навсегда.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f'⭐ Открыть PRO · {config.premium_price_stars}', callback_data=f'premium:info:{chat.id}', style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data=f'menu:group_settings:{chat.id}')],
        ]))
        return
    await callback.answer()
    await render_protection(callback.message, session, chat, key)


@router.callback_query(F.data.startswith('groupcfg:toggle:'))
async def protection_toggle(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, key, raw_chat_id, raw_enabled = callback.data.split(':')
        chat_id, enabled = int(raw_chat_id), raw_enabled == '1'
    except (AttributeError, TypeError, ValueError):
        await callback.answer('Некорректные настройки', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat or key not in PROTECTION_BY_KEY or (PROTECTION_BY_KEY[key].premium and not await chat_has_pro(session, chat_id, callback.from_user.id, config)):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    if key == 'forbidden_words' and enabled and not await forbidden_words(session, chat.id):
        await callback.answer('Сначала добавьте запрещённые слова.', show_alert=True)
        return
    await set_protection(session, chat.id, key, enabled)
    note = None
    if key == 'captcha' and not enabled:
        from app.handlers.verification import release_challenge
        pending = (await session.scalars(select(JoinVerification).where(JoinVerification.chat_id == chat.id, JoinVerification.is_verified.is_(False), JoinVerification.completed_at.is_(None)))).all()
        released = 0
        for challenge in pending:
            if await release_challenge(bot, session, challenge):
                released += 1
        note = f'🔓 Ожидавших проверку освобождено: <b>{released}</b>.' if pending else None
    await session.commit()
    await callback.answer('Включено' if enabled else 'Выключено')
    await render_protection(callback.message, session, chat, key, note=note)


@router.callback_query(F.data.startswith('groupcfg:action:'))
async def protection_action_change(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, key, raw_chat_id, action = callback.data.split(':')
        chat_id = int(raw_chat_id)
    except (AttributeError, TypeError, ValueError):
        await callback.answer('Некорректные настройки', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat or key not in PROTECTION_BY_KEY or (PROTECTION_BY_KEY[key].premium and not await chat_has_pro(session, chat_id, callback.from_user.id, config)):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    try:
        await set_protection_action(session, chat.id, key, action)
    except KeyError:
        await callback.answer('Неизвестное наказание', show_alert=True)
        return
    await session.commit()
    await callback.answer('Наказание сохранено')
    await render_protection(callback.message, session, chat, key)


@router.callback_query(F.data.startswith('groupcfg:words:'))
async def words_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat or not await chat_has_pro(session, chat_id, callback.from_user.id, config):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    await state.set_state(ForbiddenWordsForm.words)
    await state.update_data(protection_chat_id=chat.id)
    await callback.answer()
    await callback.message.edit_text('🔤 <b>ЗАПРЕЩЁННЫЕ СЛОВА</b>\n\nОтправьте слова через запятую или каждое с новой строки. Максимум 100 слов.\n\nЧтобы очистить список, отправьте <code>-</code>.', reply_markup=back_button(f'groupcfg:view:forbidden_words:{chat.id}'))


@router.message(ForbiddenWordsForm.words, F.text)
async def words_save(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data()
    chat_id = int(data.get('protection_chat_id', 0))
    chat = await managed_chat(session, bot, config, chat_id, message.from_user.id)
    if not chat or not await chat_has_pro(session, chat_id, message.from_user.id, config):
        await state.clear()
        await message.answer('Доступ к настройкам потерян.', reply_markup=back_button('menu:group_settings'))
        return
    raw = message.text.strip()
    values = [] if raw == '-' else [part.strip().casefold() for line in raw.splitlines() for part in line.split(',') if part.strip()]
    words = list(dict.fromkeys(values))[:100]
    await set_forbidden_words(session, chat.id, words)
    await session.commit()
    await state.clear()
    await message.answer(f'✅ Список сохранён: <b>{len(words)}</b> слов. Фильтр {"включён" if words else "выключен"}.', reply_markup=back_button(f'groupcfg:view:forbidden_words:{chat.id}'))


@router.callback_query(F.data.startswith('groupcfg:rules:'))
async def group_rules(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat:
        await callback.answer('Нет доступа.', show_alert=True)
        return
    row = await session.scalar(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat.id, ChatProtectionSetting.key == 'rules_text'))
    current = row.value if row and row.value else 'Используются стандартные правила владельца бота.'
    await callback.answer()
    await callback.message.edit_text(f'📜 <b>ПРАВИЛА ГРУППЫ</b>\n━━━━━━━━━━━━\n\n💬 <b>{escape(chat.title)}</b>\n\n{escape(current)}', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✏️ Изменить правила', callback_data=f'groupcfg:rules_edit:{chat.id}', style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text='⬅️ К настройкам', callback_data=f'menu:group_settings:{chat.id}')],
    ]))


@router.callback_query(F.data.startswith('groupcfg:rules_edit:'))
async def group_rules_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat:
        await callback.answer('Нет доступа.', show_alert=True)
        return
    await state.set_state(GroupRulesForm.text)
    await state.update_data(rules_chat_id=chat.id)
    await callback.answer()
    await callback.message.edit_text('📜 Отправьте новый текст правил группы — от 10 до 3500 символов.', reply_markup=back_button(f'groupcfg:rules:{chat.id}'))


@router.message(GroupRulesForm.text, F.text)
async def group_rules_save(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data()
    chat_id = int(data.get('rules_chat_id', 0))
    chat = await managed_chat(session, bot, config, chat_id, message.from_user.id)
    text = message.text.strip()
    if not chat:
        await state.clear()
        await message.answer('Доступ к группе потерян.', reply_markup=back_button('menu:group_settings'))
        return
    if not 10 <= len(text) <= 3500:
        await message.answer('Правила должны содержать от 10 до 3500 символов.')
        return
    row = await session.scalar(select(ChatProtectionSetting).where(ChatProtectionSetting.chat_id == chat.id, ChatProtectionSetting.key == 'rules_text'))
    if row:
        row.value = text
        row.enabled = True
    else:
        session.add(ChatProtectionSetting(chat_id=chat.id, key='rules_text', enabled=True, value=text))
    await session.commit()
    await state.clear()
    await message.answer('✅ Правила этой группы сохранены.', reply_markup=back_button(f'groupcfg:rules:{chat.id}'))


async def period_member_counts(session: AsyncSession, chat_id: int, since: datetime) -> tuple[int, int]:
    rows = dict((await session.execute(
        select(MemberEvent.event_type, func.count(MemberEvent.id))
        .where(MemberEvent.chat_id == chat_id, MemberEvent.occurred_at >= since)
        .group_by(MemberEvent.event_type)
    )).all())
    return int(rows.get('join', 0)), int(rows.get('leave', 0))


@router.callback_query(F.data.startswith('groupcfg:stats:'))
async def group_statistics(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await managed_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat:
        await callback.answer('Нет доступа к статистике группы.', show_alert=True)
        return
    now = datetime.now(timezone.utc)
    day_join, day_leave = await period_member_counts(session, chat.id, now - timedelta(hours=24))
    week_join, week_leave = await period_member_counts(session, chat.id, now - timedelta(days=7))
    month_join, month_leave = await period_member_counts(session, chat.id, now - timedelta(days=30))
    action_counts = dict((await session.execute(
        select(ModerationAction.action, func.count(ModerationAction.id))
        .where(ModerationAction.chat_id == chat.id)
        .group_by(ModerationAction.action)
    )).all())
    active_warnings = await session.scalar(select(func.count(Warning.id)).where(Warning.chat_id == chat.id, Warning.is_active.is_(True))) or 0
    actions = (await session.scalars(
        select(ModerationAction)
        .where(ModerationAction.chat_id == chat.id, ModerationAction.target_user_id.is_not(None), ModerationAction.action.in_((ModerationActionType.BAN, ModerationActionType.UNBAN)))
        .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
    )).all()
    latest_by_user = {}
    for action in actions:
        latest_by_user.setdefault(action.target_user_id, str(action.action))
    active_bans = sum(action == ModerationActionType.BAN for action in latest_by_user.values())
    message_rows = dict((await session.execute(
        select(DailyActivity.day, func.sum(DailyActivity.message_count))
        .where(DailyActivity.chat_id == chat.id, DailyActivity.day >= date.today() - timedelta(days=29))
        .group_by(DailyActivity.day)
    )).all())
    today_messages = int(message_rows.get(date.today(), 0) or 0)
    week_messages = sum(int(value or 0) for day, value in message_rows.items() if day >= date.today() - timedelta(days=6))
    month_messages = sum(int(value or 0) for value in message_rows.values())
    try:
        members = await bot.get_chat_member_count(chat.telegram_id)
    except Exception:
        members = '—'
    text = f'''📊 <b>СТАТИСТИКА ГРУППЫ</b>
━━━━━━━━━━━━

💬 <b>{escape(chat.title)}</b>
👥 Участников сейчас: <b>{members}</b>

<b>ВХОДЫ И ВЫХОДЫ</b>
24 часа: <b>+{day_join}</b> / <b>−{day_leave}</b>
7 дней: <b>+{week_join}</b> / <b>−{week_leave}</b>
30 дней: <b>+{month_join}</b> / <b>−{month_leave}</b>

<b>СООБЩЕНИЯ</b>
Сегодня: <b>{today_messages}</b>
7 дней: <b>{week_messages}</b>
30 дней: <b>{month_messages}</b>

<b>МОДЕРАЦИЯ ЗА ВСЁ ВРЕМЯ</b>
🚫 Банов: <b>{action_counts.get('ban', 0)}</b>
🔇 Мутов: <b>{action_counts.get('mute', 0)}</b>
⚠️ Варнов: <b>{action_counts.get('warn', 0)}</b>
🔒 Сейчас числятся в бане по журналу бота: <b>{active_bans}</b>
📌 Активных предупреждений: <b>{active_warnings}</b>

<i>История входов и выходов накапливается с момента включения этой версии.</i>'''
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔄 Обновить', callback_data=f'groupcfg:stats:{chat.id}')],
        [InlineKeyboardButton(text='⬅️ К настройкам', callback_data=f'menu:group_settings:{chat.id}')],
    ]))
