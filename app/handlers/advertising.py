from __future__ import annotations

from html import escape
import logging
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import AdvertisingPlacement, AdvertisingRequest
from app.services.parse import is_reasonable_url, normalise_url
from app.services.users import upsert_user
from app.keyboards.common import back_button
from app.services.text import user_label

router = Router(name="advertising")
log = logging.getLogger(__name__)


class AdForm(StatesGroup):
    description = State(); link = State(); placement = State(); format = State(); budget = State(); comment = State(); preview = State()


@router.callback_query(F.data == "ads:start", F.message.chat.type == "private")
async def ad_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(); await state.set_state(AdForm.description)
    await callback.message.edit_text("💼 <b>Реклама и сотрудничество</b>\n\nЧто вы хотите рекламировать? Отправьте короткое описание.")


@router.message(AdForm.description, F.text)
async def ad_description(message: Message, state: FSMContext) -> None:
    if len(message.text) > 1500:
        await message.answer('Сократите описание до 1500 символов.'); return
    await state.update_data(description=message.text); await state.set_state(AdForm.link)
    await message.answer("Отправьте ссылку на проект / продукт.")


@router.message(AdForm.link)
async def ad_link(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.text or len(message.text) > 512 or not is_reasonable_url(message.text): await message.answer("Нужна корректная ссылка до 512 символов, например https://example.com или t.me/example."); return
    await state.update_data(link=normalise_url(message.text))
    placements = (await session.scalars(select(AdvertisingPlacement).where(AdvertisingPlacement.is_active.is_(True)).order_by(AdvertisingPlacement.sort_order))).all()
    # A local V1 installation can accept cooperation requests before an owner
    # has configured named placements; the request still reaches the owner.
    if not placements:
        fallback = AdvertisingPlacement(name="Обсудить с владельцем", is_active=True, sort_order=0)
        session.add(fallback)
        await session.flush()
        placements = [fallback]
    await state.set_state(AdForm.placement)
    await message.answer("В каком проекте хотите разместить рекламу?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=p.name, callback_data=f"ads:placement:{p.id}")] for p in placements]))


@router.callback_query(AdForm.placement, F.data.startswith("ads:placement:"))
async def ad_placement(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer(); await state.update_data(placement_id=int(callback.data.rsplit(":", 1)[1])); await state.set_state(AdForm.format)
    buttons = [[InlineKeyboardButton(text=x, callback_data=f"ads:format:{x}")] for x in ["📝 Пост", "📌 Закреп", "📣 Упоминание", "💬 Индивидуально"]]
    await callback.message.edit_text("Какой формат рекламы вас интересует?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(AdForm.format, F.data.startswith("ads:format:"))
async def ad_format(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer(); await state.update_data(format=callback.data.split(":", 2)[2]); await state.set_state(AdForm.budget)
    await callback.message.edit_text("Какой бюджет вы предлагаете?")


@router.message(AdForm.budget, F.text)
async def ad_budget(message: Message, state: FSMContext) -> None:
    if len(message.text) > 100:
        await message.answer('Укажите бюджет короче: до 100 символов.'); return
    await state.update_data(budget=message.text); await state.set_state(AdForm.comment)
    await message.answer("Дополнительный комментарий?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="ads:skip_comment")]]))


async def show_preview(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data(); placement = await session.get(AdvertisingPlacement, data["placement_id"])
    text = f"💼 <b>Ваша заявка</b>\n\nПроект: {escape(placement.name) if placement else '—'}\nЧто рекламируется: {escape(data['description'])}\nСсылка: {escape(data['link'])}\nФормат: {escape(data['format'])}\nБюджет: {escape(data['budget'])}\nКомментарий: {escape(data.get('comment') or '—')}"
    await state.set_state(AdForm.preview); await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Отправить", callback_data="ads:send", style=ButtonStyle.SUCCESS)], [InlineKeyboardButton(text="✏️ Начать заново", callback_data="ads:start"), InlineKeyboardButton(text="❌ Отмена", callback_data="ads:cancel")]]))


@router.message(AdForm.comment, F.text)
async def ad_comment(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if len(message.text) > 1000:
        await message.answer('Сократите комментарий до 1000 символов.'); return
    await state.update_data(comment=message.text); await show_preview(message, state, session)


@router.callback_query(AdForm.comment, F.data == "ads:skip_comment")
async def ad_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer(); await state.update_data(comment=None); await show_preview(callback.message, state, session)


@router.callback_query(AdForm.preview, F.data == "ads:send")
async def ad_send(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data(); applicant = await upsert_user(session, callback.from_user)
    placement = await session.get(AdvertisingPlacement, data['placement_id'])
    request = AdvertisingRequest(applicant_user_id=applicant.id, **data)
    session.add(request); await session.commit(); await state.clear(); await callback.answer("Заявка отправлена")
    await callback.message.edit_text(f"✅ Заявка #{request.id} отправлена владельцу. Мы свяжемся с вами здесь.", reply_markup=back_button('nav:private_main'))
    for owner_id in config.owner_ids:
        try: await bot.send_message(owner_id, f"💼 <b>Новая рекламная заявка #{request.id}</b>\n\nОт кого: {user_label(callback.from_user)}\nКуда: {escape(placement.name) if placement else 'не указано'}\nЧто рекламируется: {escape(request.description)}\nСсылка: {escape(request.link)}\nФормат: {escape(request.format)}\nБюджет: {escape(request.budget)}\nКомментарий: {escape(request.comment or '—')}", reply_markup=back_button('admin:home'))
        except TelegramAPIError:
            log.warning('Could not notify owner %s about advertising request %s', owner_id, request.id, exc_info=True)


@router.callback_query(F.data == "ads:cancel")
async def ad_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear(); await callback.answer(); await callback.message.edit_text("Заявка отменена.")


@router.message(AdForm.description, ~F.text)
@router.message(AdForm.budget, ~F.text)
@router.message(AdForm.comment, ~F.text)
async def ad_text_required(message: Message) -> None:
    await message.answer('На этом шаге нужен текст, не фото или видео.')
