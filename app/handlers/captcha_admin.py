from __future__ import annotations

from html import escape
from math import ceil

from aiogram import F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import CaptchaQuestion
from app.services.users import upsert_user
from app.services.verification import active_custom_count, auto_math_enabled, generate_math_question, set_auto_math_enabled


router = Router(name='captcha_admin')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')

PAGE_SIZE = 8


class CaptchaQuestionForm(StatesGroup):
    question = State()
    option = State()
    correct = State()


async def owner_only(event: Message | CallbackQuery, config: Settings) -> bool:
    if config.is_owner(event.from_user.id):
        return False
    if isinstance(event, CallbackQuery):
        await event.answer('Этот раздел доступен только владельцу бота.', show_alert=True)
    else:
        await event.answer('Этот раздел доступен только владельцу бота.')
    return True


async def render_panel(message: Message, session: AsyncSession, *, note: str | None = None) -> None:
    automatic = await auto_math_enabled(session)
    custom_count = await active_custom_count(session)
    text = f'''🧠 <b>КАПЧА НОВИЧКОВ</b>
━━━━━━━━━━━━

🤖 Автоматические примеры: {"✅ <b>включены</b>" if automatic else "❌ <b>выключены</b>"}
🗂 Своих вопросов: <b>{custom_count}</b>

Автоматический генератор создаёт новый пример на сложение для каждой проверки. Ответ всегда от <b>2 до 50</b>; под вопросом появляются четыре разных варианта.

Если добавить свои вопросы, бот будет использовать их вместе с примерами. При выключенном генераторе останутся только собственные вопросы.

<i>Здесь настраивается набор заданий. Саму капчу для конкретной группы включайте в «Настройки группы».</i>'''
    if note:
        text += f'\n\n{note}'
    rows = [[InlineKeyboardButton(
        text='❌ Выключить автопримеры' if automatic else '✅ Включить автопримеры',
        callback_data=f'capadmin:auto:{0 if automatic else 1}',
        style=ButtonStyle.SUCCESS if not automatic else None,
    )]]
    rows.append([
        InlineKeyboardButton(text='➕ Добавить вопрос', callback_data='capadmin:add'),
        InlineKeyboardButton(text='🗂 Мои вопросы', callback_data='capadmin:list:0'),
    ])
    rows.append([InlineKeyboardButton(text='🎲 Проверить генератор', callback_data='capadmin:preview')])
    rows.append([InlineKeyboardButton(text='⬅️ В супер-админку', callback_data='admin:home')])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == 'admin:captcha')
async def captcha_panel(callback: CallbackQuery, session: AsyncSession, config: Settings, state: FSMContext) -> None:
    if await owner_only(callback, config):
        return
    await state.clear()
    await callback.answer()
    await render_panel(callback.message, session)


@router.callback_query(F.data.startswith('capadmin:auto:'))
async def toggle_automatic(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    raw_enabled = callback.data.rsplit(':', 1)[-1]
    if raw_enabled not in {'0', '1'}:
        await callback.answer('Некорректная настройка.', show_alert=True)
        return
    enabled = raw_enabled == '1'
    if not enabled and await active_custom_count(session) == 0:
        await callback.answer('Сначала добавьте хотя бы один свой вопрос.', show_alert=True)
        return
    await set_auto_math_enabled(session, enabled)
    await session.commit()
    await callback.answer('Автопримеры включены' if enabled else 'Автопримеры выключены')
    await render_panel(callback.message, session)


@router.callback_query(F.data == 'capadmin:preview')
async def preview_generator(callback: CallbackQuery, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    question = generate_math_question()
    lines = [
        '🎲 <b>ПРИМЕР АВТОМАТИЧЕСКОЙ КАПЧИ</b>',
        '━━━━━━━━━━━━',
        '',
        f'<b>{escape(question.text)}</b>',
        '',
    ]
    for index, option in enumerate(question.options):
        marker = '✅' if index == question.correct_index else '▫️'
        lines.append(f'{marker} {index + 1}. <code>{escape(option)}</code>')
    lines.extend(['', '<i>У новичка правильный вариант не будет отмечен.</i>'])
    await callback.answer()
    await callback.message.edit_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🎲 Другой пример', callback_data='capadmin:preview'),
        InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:captcha'),
    ]]))


@router.callback_query(F.data == 'capadmin:add')
async def add_question(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    await state.clear()
    await state.set_state(CaptchaQuestionForm.question)
    await callback.answer()
    await callback.message.edit_text(
        '➕ <b>НОВЫЙ ВОПРОС</b>\n━━━━━━━━━━━━\n\nОтправьте текст вопроса — от 3 до 500 символов.\n\n<i>Например: Какого цвета трава летом?</i>',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='❌ Отмена', callback_data='admin:captcha'),
        ]]),
    )


@router.message(CaptchaQuestionForm.question, F.text)
async def receive_question(message: Message, state: FSMContext, config: Settings) -> None:
    if await owner_only(message, config):
        await state.clear()
        return
    value = message.text.strip()
    if not 3 <= len(value) <= 500:
        await message.answer('Текст вопроса должен содержать от 3 до 500 символов. Отправьте другой текст.')
        return
    await state.update_data(question=value, options=[])
    await state.set_state(CaptchaQuestionForm.option)
    await message.answer('1️⃣ Отправьте <b>первый вариант ответа</b> — до 80 символов.')


@router.message(CaptchaQuestionForm.question)
async def receive_question_non_text(message: Message) -> None:
    await message.answer('Нужен текст вопроса. Отправьте обычное текстовое сообщение.')


@router.message(CaptchaQuestionForm.option, F.text)
async def receive_option(message: Message, state: FSMContext, config: Settings) -> None:
    if await owner_only(message, config):
        await state.clear()
        return
    value = message.text.strip()
    if not 1 <= len(value) <= 80:
        await message.answer('Ответ должен содержать от 1 до 80 символов. Отправьте другой вариант.')
        return
    data = await state.get_data()
    options = list(data.get('options', []))
    if value.casefold() in {option.casefold() for option in options}:
        await message.answer('Такой вариант уже есть. Все четыре ответа должны отличаться.')
        return
    options.append(value)
    await state.update_data(options=options)
    if len(options) < 4:
        numbers = ('2️⃣', '3️⃣', '4️⃣')
        await message.answer(f'{numbers[len(options) - 1]} Отправьте <b>{("второй", "третий", "четвёртый")[len(options) - 1]} вариант ответа</b>.')
        return
    await state.set_state(CaptchaQuestionForm.correct)
    rows = [[InlineKeyboardButton(
        text=f'{index + 1}. {option[:48]}',
        callback_data=f'capadmin:correct:{index}',
    )] for index, option in enumerate(options)]
    rows.append([InlineKeyboardButton(text='❌ Отмена', callback_data='admin:captcha')])
    await message.answer(
        '✅ Получены четыре варианта. Теперь выберите <b>правильный ответ</b>.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(CaptchaQuestionForm.option)
async def receive_option_non_text(message: Message) -> None:
    await message.answer('Нужен текст ответа. Отправьте обычное текстовое сообщение.')


@router.callback_query(CaptchaQuestionForm.correct, F.data.startswith('capadmin:correct:'))
async def save_question(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        await state.clear()
        return
    try:
        correct_index = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректный ответ', show_alert=True)
        return
    data = await state.get_data()
    question = str(data.get('question', '')).strip()
    options = list(data.get('options', []))
    if not question or len(options) != 4 or not 0 <= correct_index <= 3:
        await state.clear()
        await callback.answer('Форма устарела. Добавьте вопрос заново.', show_alert=True)
        await render_panel(callback.message, session)
        return
    creator = await upsert_user(session, callback.from_user)
    row = CaptchaQuestion(
        question=question,
        options=options,
        correct_index=correct_index,
        created_by_user_id=creator.id,
    )
    session.add(row)
    await session.commit()
    await state.clear()
    await callback.answer('Вопрос сохранён')
    await render_panel(callback.message, session, note='✅ Собственный вопрос сохранён и уже участвует в проверках.')


async def render_question_list(message: Message, session: AsyncSession, page: int) -> None:
    total = int(await session.scalar(select(func.count(CaptchaQuestion.id)).where(CaptchaQuestion.is_active.is_(True))) or 0)
    page_count = max(1, ceil(total / PAGE_SIZE))
    page = max(0, min(page, page_count - 1))
    rows_data = (
        await session.scalars(
            select(CaptchaQuestion)
            .where(CaptchaQuestion.is_active.is_(True))
            .order_by(CaptchaQuestion.id.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    rows = [[InlineKeyboardButton(
        text=f'#{row.id} · {" ".join(row.question.split())[:48]}',
        callback_data=f'capadmin:view:{row.id}:{page}',
    )] for row in rows_data]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text='⬅️', callback_data=f'capadmin:list:{page - 1}'))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton(text='➡️', callback_data=f'capadmin:list:{page + 1}'))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text='➕ Добавить вопрос', callback_data='capadmin:add')])
    rows.append([InlineKeyboardButton(text='⬅️ К капче', callback_data='admin:captcha')])
    text = '🗂 <b>СВОИ ВОПРОСЫ</b>\n━━━━━━━━━━━━\n\n'
    text += f'Активных вопросов: <b>{total}</b> · страница <b>{page + 1}/{page_count}</b>.\n\nНажмите вопрос, чтобы увидеть ответы или удалить его.' if total else 'Список пока пуст. Добавьте первый вопрос или оставьте автоматические примеры включёнными.'
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('capadmin:list:'))
async def question_list(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    try:
        page = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        page = 0
    await callback.answer()
    await render_question_list(callback.message, session, page)


@router.callback_query(F.data.startswith('capadmin:view:'))
async def view_question(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    try:
        _, _, raw_id, raw_page = callback.data.split(':')
        question_id, page = int(raw_id), int(raw_page)
    except (AttributeError, ValueError):
        await callback.answer('Некорректный вопрос', show_alert=True)
        return
    row = await session.get(CaptchaQuestion, question_id)
    if not row or not row.is_active:
        await callback.answer('Вопрос уже удалён.', show_alert=True)
        await render_question_list(callback.message, session, page)
        return
    lines = [f'🧠 <b>ВОПРОС #{row.id}</b>', '━━━━━━━━━━━━', '', f'<b>{escape(row.question)}</b>', '']
    for index, option in enumerate(row.options):
        marker = '✅' if index == row.correct_index else '▫️'
        lines.append(f'{marker} {index + 1}. {escape(option)}')
    await callback.answer()
    await callback.message.edit_text('\n'.join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🗑 Удалить вопрос', callback_data=f'capadmin:delete_confirm:{row.id}:{page}')],
        [InlineKeyboardButton(text='⬅️ К списку', callback_data=f'capadmin:list:{page}')],
    ]))


@router.callback_query(F.data.startswith('capadmin:delete_confirm:'))
async def confirm_delete(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    try:
        _, _, raw_id, raw_page = callback.data.split(':')
        question_id, page = int(raw_id), int(raw_page)
    except (AttributeError, ValueError):
        await callback.answer('Некорректный вопрос', show_alert=True)
        return
    row = await session.get(CaptchaQuestion, question_id)
    if not row or not row.is_active:
        await callback.answer('Вопрос уже удалён.', show_alert=True)
        await render_question_list(callback.message, session, page)
        return
    await callback.answer()
    await callback.message.edit_text(
        f'🗑 <b>УДАЛИТЬ ВОПРОС?</b>\n\n{escape(row.question)}\n\nВопрос перестанет выдаваться новым участникам.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🗑 Да, удалить', callback_data=f'capadmin:delete:{row.id}:{page}')],
            [InlineKeyboardButton(text='⬅️ Не удалять', callback_data=f'capadmin:view:{row.id}:{page}')],
        ]),
    )


@router.callback_query(F.data.startswith('capadmin:delete:'))
async def delete_question(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await owner_only(callback, config):
        return
    try:
        _, _, raw_id, raw_page = callback.data.split(':')
        question_id, page = int(raw_id), int(raw_page)
    except (AttributeError, ValueError):
        await callback.answer('Некорректный вопрос', show_alert=True)
        return
    row = await session.get(CaptchaQuestion, question_id, with_for_update=True)
    if not row or not row.is_active:
        await callback.answer('Вопрос уже удалён.', show_alert=True)
        await render_question_list(callback.message, session, page)
        return
    if not await auto_math_enabled(session) and await active_custom_count(session) <= 1:
        await callback.answer('Нельзя удалить последний вопрос, пока автопримеры выключены.', show_alert=True)
        return
    row.is_active = False
    await session.commit()
    await callback.answer('Вопрос удалён')
    await render_question_list(callback.message, session, page)
