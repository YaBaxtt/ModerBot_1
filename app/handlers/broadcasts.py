from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import Broadcast, BroadcastTarget
from app.services.broadcasts import broadcast_markup, deliver_broadcast, schedule_delivery
from app.services.parse import is_reasonable_url, normalise_url
from app.services.users import upsert_user
from app.keyboards.common import admin_menu

router = Router(name="broadcasts")
TARGETS = {"users": BroadcastTarget.USERS, "chats": BroadcastTarget.ALL_CHATS}


class BroadcastForm(StatesGroup):
    text = State()
    photo = State()
    link = State()
    button_text = State()
    preview = State()
    schedule = State()


def cancel_markup(*, skip: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if skip:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data=skip)])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def owner_only(callback: CallbackQuery, config: Settings) -> bool:
    if config.is_owner(callback.from_user.id):
        return True
    await callback.answer("Нет доступа", show_alert=True)
    return False


@router.callback_query(F.data == "broadcast:start", F.message.chat.type == "private")
async def start(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("📢 <b>Новая рассылка</b>\n\nКуда отправить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователям бота", callback_data="broadcast:target:users")],
        [InlineKeyboardButton(text="💬 Во все подключённые чаты", callback_data="broadcast:target:chats")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ]))


@router.callback_query(F.data.startswith("broadcast:target:"))
async def choose_target(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    target = callback.data.rsplit(":", 1)[1]
    if target not in TARGETS:
        await callback.answer("Некорректный получатель", show_alert=True)
        return
    await state.update_data(target=target)
    await state.set_state(BroadcastForm.text)
    await callback.answer()
    await callback.message.edit_text("📝 Отправьте текст для рассылки.", reply_markup=cancel_markup())


@router.message(BroadcastForm.text, F.text)
async def broadcast_text(message: Message, state: FSMContext, config: Settings) -> None:
    if not config.is_owner(message.from_user.id):
        return
    if len(message.html_text) > 4000:
        await message.answer("Текст слишком длинный. Сократите его до 4000 символов с учётом форматирования.")
        return
    await state.update_data(text=message.html_text)
    await state.set_state(BroadcastForm.photo)
    await message.answer("🖼 Отправьте фото для рассылки или пропустите этот шаг.", reply_markup=cancel_markup(skip="broadcast:skip_photo"))


@router.message(BroadcastForm.photo, F.photo)
async def broadcast_photo(message: Message, state: FSMContext, config: Settings) -> None:
    if not config.is_owner(message.from_user.id):
        return
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await ask_link(message, state)


@router.callback_query(BroadcastForm.photo, F.data == "broadcast:skip_photo")
async def skip_photo(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    await state.update_data(photo_file_id=None)
    await callback.answer()
    await ask_link(callback.message, state)


async def ask_link(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.link)
    await message.answer("🔗 Отправьте ссылку для кнопки или пропустите этот шаг.", reply_markup=cancel_markup(skip="broadcast:skip_link"))


@router.message(BroadcastForm.link, F.text)
async def broadcast_link(message: Message, state: FSMContext, config: Settings) -> None:
    if not config.is_owner(message.from_user.id):
        return
    if not is_reasonable_url(message.text):
        await message.answer("Нужна корректная ссылка: https://example.com или t.me/example.")
        return
    await state.update_data(button_url=normalise_url(message.text))
    await state.set_state(BroadcastForm.button_text)
    await message.answer("Введите название кнопки.", reply_markup=cancel_markup())


@router.callback_query(BroadcastForm.link, F.data == "broadcast:skip_link")
async def skip_link(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    await state.update_data(button_url=None, button_text=None)
    await callback.answer()
    await show_preview(callback.message, state)


@router.message(BroadcastForm.button_text, F.text)
async def broadcast_button_text(message: Message, state: FSMContext, config: Settings) -> None:
    if not config.is_owner(message.from_user.id):
        return
    label = message.text.strip()
    if not label or len(label) > 64:
        await message.answer("Название кнопки должно содержать от 1 до 64 символов.")
        return
    await state.update_data(button_text=label)
    await show_preview(message, state)


async def show_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(BroadcastForm.preview)
    target_label = "пользователям бота" if data["target"] == "users" else "подключённым чатам"
    await message.answer(f"👁 <b>Предпросмотр рассылки</b>\nПолучатели: {target_label}")
    preview = data['text']
    content_markup = broadcast_markup(data.get('button_text'), data.get('button_url'))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="broadcast:confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ])
    if data.get("photo_file_id"):
        if len(preview) > 1024:
            await message.answer_photo(data["photo_file_id"])
            await message.answer(preview, reply_markup=content_markup)
        else:
            await message.answer_photo(data["photo_file_id"], caption=preview, reply_markup=content_markup)
    else:
        await message.answer(preview, reply_markup=content_markup)
    await message.answer("Всё верно?", reply_markup=markup)


@router.callback_query(BroadcastForm.preview, F.data == "broadcast:confirm")
async def confirm(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    await state.set_state(BroadcastForm.schedule)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data="broadcast:send:now")],
        [InlineKeyboardButton(text="🕐 Через 10 минут", callback_data="broadcast:send:10m"), InlineKeyboardButton(text="🕐 Через час", callback_data="broadcast:send:1h")],
        [InlineKeyboardButton(text="📅 Точная дата и время", callback_data="broadcast:send:custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast:cancel")],
    ]))


@router.callback_query(BroadcastForm.schedule, F.data.startswith("broadcast:send:"))
async def schedule_choice(callback: CallbackQuery, state: FSMContext, config: Settings, session: AsyncSession, bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    if not await owner_only(callback, config):
        return
    choice = callback.data.rsplit(":", 1)[1]
    if choice not in {"now", "10m", "1h", "custom"}:
        await callback.answer("Неизвестное время отправки.", show_alert=True)
        return
    if choice == "custom":
        await callback.answer()
        await callback.message.answer("Введите дату и время в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.", reply_markup=cancel_markup())
        return
    scheduled_at = None if choice == "now" else datetime.now() + (timedelta(minutes=10) if choice == "10m" else timedelta(hours=1))
    await create_and_dispatch(callback, state, session, bot, session_factory, scheduled_at)


@router.message(BroadcastForm.schedule, F.text)
async def custom_schedule(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, session_factory: async_sessionmaker[AsyncSession], config: Settings) -> None:
    if not config.is_owner(message.from_user.id):
        return
    try:
        scheduled_at = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("Используйте формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.")
        return
    if scheduled_at <= datetime.now():
        await message.answer("Укажите будущую дату и время.")
        return
    await create_and_dispatch(message, state, session, bot, session_factory, scheduled_at)


async def create_and_dispatch(event: CallbackQuery | Message, state: FSMContext, session: AsyncSession, bot: Bot, session_factory: async_sessionmaker[AsyncSession], scheduled_at: datetime | None) -> None:
    data = await state.get_data()
    if not data.get('text') or data.get('target') not in TARGETS:
        if isinstance(event, CallbackQuery):
            await event.answer("Эта рассылка уже обработана. Откройте панель заново.", show_alert=True)
        return
    if isinstance(event, CallbackQuery):
        await event.answer("Сохраняю рассылку…")
    creator = await upsert_user(session, event.from_user)
    broadcast = Broadcast(creator_user_id=creator.id, target=TARGETS[data["target"]], text=data["text"], photo_file_id=data.get("photo_file_id"), button_text=data.get("button_text"), button_url=data.get("button_url"), scheduled_at=scheduled_at)
    session.add(broadcast)
    broadcast.scheduled_at = scheduled_at or datetime.now()
    await session.commit()
    await state.clear()
    schedule_delivery(bot, session_factory, broadcast.id, broadcast.scheduled_at)
    if scheduled_at:
        text = f"✅ Рассылка #{broadcast.id} запланирована на {scheduled_at.strftime('%d.%m.%Y %H:%M')}."
    else:
        text = f"✅ Рассылка #{broadcast.id} сохранена и отправляется. Итог придёт сюда."
    if isinstance(event, CallbackQuery):
        await event.message.edit_reply_markup(reply_markup=None)
        await event.message.answer(text, reply_markup=admin_menu())
    else:
        await event.answer(text, reply_markup=admin_menu())


@router.callback_query(F.data == "broadcast:cancel")
async def cancel(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if not await owner_only(callback, config):
        return
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Рассылка отменена.", reply_markup=admin_menu())
