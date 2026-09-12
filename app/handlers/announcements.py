"""Owner-created, explicitly confirmed pinned announcements for one chat."""
import asyncio
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, Setting
from app.keyboards.common import back_button

router = Router(name='announcements')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
locks: dict[int, asyncio.Lock] = {}


class AnnouncementForm(StatesGroup):
    text = State()
    preview = State()


@router.callback_query(F.data == 'announce:start')
async def start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Settings):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    await state.clear()
    await callback.answer()
    chats = (await session.scalars(select(Chat).where(Chat.is_active.is_(True)))).all()
    rows = [[InlineKeyboardButton(text=chat.title[:64], callback_data=f'announce:chat:{chat.id}')] for chat in chats]
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')])
    await callback.message.answer('📌 Выберите чат для объявления.' if chats else 'Нет подключённых чатов.', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('announce:chat:'))
async def choose_chat(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Settings):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    chat = await session.get(Chat, int(callback.data.rsplit(':', 1)[1]))
    if not chat or not chat.is_active:
        await callback.answer('Чат недоступен', show_alert=True); return
    await state.clear()
    await state.update_data(announcement_chat=chat.id)
    await state.set_state(AnnouncementForm.text)
    await callback.answer()
    await callback.message.answer(f'📌 Чат: <b>{escape(chat.title)}</b>\nОтправьте текст объявления (до 3500 символов). После подтверждения оно будет опубликовано и закреплено. Старое объявление бота будет откреплено, но не удалено.', reply_markup=back_button('admin:home'))


@router.message(AnnouncementForm.text, F.text)
async def text(message: Message, state: FSMContext, config: Settings):
    if not config.is_owner(message.from_user.id): return
    if not message.text.strip() or len(message.text) > 3500:
        await message.answer('Нужен непустой текст до 3500 символов.', reply_markup=back_button('admin:home')); return
    await state.update_data(announcement_text=message.html_text)
    await state.set_state(AnnouncementForm.preview)
    await message.answer(message.html_text)
    await message.answer('Опубликовать и закрепить в выбранном чате?', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='📌 Опубликовать', callback_data='announce:publish')], [
        InlineKeyboardButton(text='⬅️ В меню / отменить', callback_data='admin:home')]]))


async def publish_announcement(bot, session, chat, content):
    async with locks.setdefault(chat.id, asyncio.Lock()):
        member = await bot.get_chat_member(chat.telegram_id, bot.id)
        if member.status != 'creator' and (member.status != 'administrator' or not getattr(member, 'can_pin_messages', False)):
            return '❌ Не опубликовано: боту нужны права администратора и «Закреплять сообщения».'
        key = f'announcement_pin:{chat.telegram_id}'
        previous = await session.get(Setting, key, populate_existing=True)
        old_id = int(previous.value) if previous and previous.value.isdigit() else None
        sent = await bot.send_message(chat.telegram_id, content)
        try:
            await bot.pin_chat_message(chat.telegram_id, sent.message_id, disable_notification=True)
        except TelegramAPIError:
            return '⚠️ Объявление отправлено, но закрепить не удалось. Предыдущий закреп не тронут. Проверьте права бота.'
        if previous:
            previous.value = str(sent.message_id)
        else:
            session.add(Setting(key=key, value=str(sent.message_id)))
        await session.commit()
        if old_id and old_id != sent.message_id:
            try:
                await bot.unpin_chat_message(chat.telegram_id, message_id=old_id)
            except TelegramAPIError:
                return '⚠️ Новое объявление закреплено, но прежнее не удалось открепить. Открепите его вручную.'
        return '✅ Объявление опубликовано и закреплено. Чужие закрепы не изменены.'


@router.callback_query(AnnouncementForm.preview, F.data == 'announce:publish')
async def publish(callback: CallbackQuery, state: FSMContext, session: AsyncSession, config: Settings, bot: Bot):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    data = await state.get_data()
    chat = await session.get(Chat, data['announcement_chat'])
    if not chat or not chat.is_active:
        await callback.answer('Чат недоступен', show_alert=True); return
    await callback.answer('Публикую…')
    # Consume confirmation before external I/O; an old button cannot resend.
    await state.clear()
    try:
        result = await publish_announcement(bot, session, chat, data['announcement_text'])
    except TelegramAPIError:
        result = '⚠️ Telegram не подтвердил отправку. Проверьте чат перед повторной публикацией.'
    await callback.message.edit_text(result, reply_markup=back_button('admin:home'))


@router.message(AnnouncementForm.text, ~F.text)
async def invalid(message: Message):
    await message.answer('Для объявления сейчас нужен текст.', reply_markup=back_button('admin:home'))
