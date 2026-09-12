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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatModerator, PrivateReport, User
from app.keyboards.common import back_button
from app.services.users import upsert_user
from app.services.text import user_label

router = Router(name='private_reports')
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')
log = logging.getLogger(__name__)
MAX_EVIDENCE = 10


class PrivateReportForm(StatesGroup):
    target = State()
    reason = State()
    media = State()
    preview = State()


def report_actions(report_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Рассмотрено', callback_data=f'private_report:review:{report_id}', style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text='❌ Закрыть', callback_data=f'private_report:close:{report_id}')],
        [InlineKeyboardButton(text='⬅️ В меню', callback_data='admin:home')]])


def media_controls(count=0):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Готово' if count else 'Пропустить (cancel)', callback_data='private_report:media_done')],
        [InlineKeyboardButton(text='⬅️ В меню', callback_data='nav:private_main')]])


async def target_is_protected(username: str, session: AsyncSession, bot: Bot, config: Settings) -> bool:
    known = await session.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if known and known.telegram_id in config.owner_ids:
        return True
    if known and await session.scalar(select(ChatModerator.id).where(ChatModerator.user_id == known.id, ChatModerator.is_active.is_(True))):
        return True
    for owner_id in config.owner_ids:
        try:
            owner = await bot.get_chat(owner_id)
            if owner.username and owner.username.lower() == username.lower():
                return True
        except Exception:
            pass
    chats = (await session.scalars(select(Chat).where(Chat.is_active.is_(True)).limit(100))).all()
    for chat in chats:
        try:
            admins = await bot.get_chat_administrators(chat.telegram_id)
            if any(member.user.username and member.user.username.lower() == username.lower() for member in admins):
                return True
        except Exception:
            continue
    return False


async def accept_target(message, state, text, session, bot, config):
    username = text.strip().lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username):
        await message.answer('Введите username человека, например <code>@username</code>. Ссылки и имена без username не подходят.', reply_markup=back_button())
        return
    if message.from_user.username and username.lower() == message.from_user.username.lower():
        await message.answer('Нельзя пожаловаться на самого себя.', reply_markup=back_button())
        return
    if await target_is_protected(username, session, bot, config):
        await message.answer('На владельцев бота и администраторов подключённых групп отправлять жалобы нельзя.', reply_markup=back_button())
        return
    await state.update_data(target_username=username, evidence=[])
    await state.set_state(PrivateReportForm.reason)
    await message.answer('📝 <b>Опишите, что произошло</b>\nНа кого и почему жалуетесь? Укажите чат, примерное время и факты. При краже — что было обещано и что потеряно. До 1500 символов.\n\nВладельцы увидят ваше имя, username (если есть) и Telegram ID, чтобы связаться с вами. Не добавляйте пароли или коды входа.', reply_markup=back_button())


@router.message(Command('report'))
async def start_report(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings):
    await state.clear()
    await state.set_state(PrivateReportForm.target)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        await accept_target(message, state, parts[1], session, bot, config)
    else:
        await message.answer('🚨 На кого жалоба? Отправьте <code>@username</code> человека.', reply_markup=back_button())


@router.callback_query(F.data == 'menu:report_help')
async def report_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(PrivateReportForm.target)
    await callback.answer()
    await callback.message.answer('🚨 <b>Личная жалоба</b>\nОтправьте <code>@username</code> человека, на которого хотите пожаловаться.', reply_markup=back_button())


@router.message(PrivateReportForm.target, F.text)
async def report_target(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings):
    await accept_target(message, state, message.text, session, bot, config)


@router.message(PrivateReportForm.reason, F.text)
async def report_reason(message: Message, state: FSMContext):
    text = message.text.strip()
    if not 5 <= len(text) <= 1500:
        await message.answer('Опишите ситуацию: от 5 до 1500 символов.', reply_markup=back_button())
        return
    await state.update_data(reason=text)
    await state.set_state(PrivateReportForm.media)
    await message.answer('📎 Прикрепите до 10 фото или видео — по одному или альбомом. После загрузки нажмите «Готово». Доказательства необязательны: «Пропустить» или /cancel переходит к подтверждению.\n\nПроверьте, нет ли на материалах личных данных.', reply_markup=media_controls())


@router.message(PrivateReportForm.media, F.photo | F.video)
async def evidence(message: Message, state: FSMContext):
    data = await state.get_data()
    items = list(data.get('evidence', []))
    if len(items) >= MAX_EVIDENCE:
        if not data.get('evidence_limit_warned'):
            await message.answer('Можно прикрепить максимум 10 файлов. Нажмите «Готово».', reply_markup=media_controls(len(items)))
            await state.update_data(evidence_limit_warned=True)
        return
    kind, file_id = ('photo', message.photo[-1].file_id) if message.photo else ('video', message.video.file_id)
    items.append({'type': kind, 'file_id': file_id})
    await state.update_data(evidence=items)
    # Album updates arrive separately; one receipt per album avoids bot spam.
    if not message.media_group_id or data.get('last_album') != message.media_group_id:
        await message.answer('Файлы принимаются. Когда загрузка закончится, нажмите «Готово».', reply_markup=media_controls(len(items)))
    await state.update_data(last_album=message.media_group_id)


async def preview(message, state):
    data = await state.get_data()
    await state.set_state(PrivateReportForm.preview)
    await message.answer(f"🚨 <b>Проверьте жалобу</b>\nНа: <a href=\"https://t.me/{escape(data['target_username'], quote=True)}\">@{escape(data['target_username'])}</a>\n\n{escape(data['reason'])}\n\nФайлов: {len(data.get('evidence', []))}. Владельцы увидят ваши контактные данные.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Отправить', callback_data='private_report:send')], [
        InlineKeyboardButton(text='⬅️ В меню / отменить', callback_data='nav:private_main')]]))


@router.callback_query(PrivateReportForm.media, F.data == 'private_report:media_done')
async def media_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await preview(callback.message, state)


@router.message(PrivateReportForm.media, Command('cancel'))
@router.message(PrivateReportForm.media, F.text.casefold() == 'cancel')
async def skip_evidence(message: Message, state: FSMContext):
    await preview(message, state)


async def deliver_private_report(bot, owner_id, row, reporter=None):
    author = user_label(reporter) if reporter else 'пользователь удалён из базы'
    target = escape(row.target_username, quote=True)
    header = f'🚨 <b>Личная жалоба #{row.id}</b>\nОт кого: {author}\nНа кого: <a href="https://t.me/{target}">@{escape(row.target_username)}</a>'
    full_text = f'{header}\n\n{escape(row.reason)}\n\nФайлов: {len(row.evidence)}'
    if not row.evidence:
        await bot.send_message(owner_id, full_text, reply_markup=report_actions(row.id))
        return
    # Telegram captions are shorter than messages. Keep the complaint attached
    # to the first proof and send a full continuation only for a long reason.
    short_reason = row.reason if len(row.reason) <= 400 else row.reason[:397] + '…'
    caption = f'{header}\n\n{escape(short_reason)}\n\nФайлов: {len(row.evidence)}'
    first, *remaining = row.evidence
    if remaining:
        album = []
        for index, media in enumerate(row.evidence):
            kwargs = {'media': media['file_id']}
            if index == 0:
                kwargs['caption'] = caption
            media_cls = InputMediaPhoto if media['type'] == 'photo' else InputMediaVideo
            album.append(media_cls(**kwargs))
        await bot.send_media_group(owner_id, media=album)
        action_text = f'🛠 <b>Действия по жалобе #{row.id}</b>'
        if len(row.reason) > 400:
            action_text = f'<b>Полное описание жалобы #{row.id}</b>\n\n{escape(row.reason)}'
        await bot.send_message(owner_id, action_text, reply_markup=report_actions(row.id))
        return
    if first['type'] == 'photo':
        await bot.send_photo(owner_id, first['file_id'], caption=caption, reply_markup=report_actions(row.id))
    else:
        await bot.send_video(owner_id, first['file_id'], caption=caption, reply_markup=report_actions(row.id))
    if len(row.reason) > 400:
        await bot.send_message(owner_id, f'<b>Полное описание жалобы #{row.id}</b>\n\n{escape(row.reason)}', reply_markup=back_button('admin:home'))


@router.callback_query(PrivateReportForm.preview, F.data == 'private_report:send')
async def submit(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings):
    data = await state.get_data()
    if await target_is_protected(data['target_username'], session, bot, config):
        await state.clear()
        await callback.answer('Жалоба запрещена', show_alert=True)
        await callback.message.edit_text('На владельцев бота и администраторов подключённых групп отправлять жалобы нельзя.', reply_markup=back_button())
        return
    reporter = await upsert_user(session, callback.from_user)
    row = PrivateReport(reporter_user_id=reporter.id, target_username=data['target_username'], reason=data['reason'], evidence=data.get('evidence', []))
    session.add(row)
    await session.commit()
    await state.clear()
    await callback.answer('Жалоба сохранена')
    await callback.message.edit_text(f'✅ Жалоба #{row.id} сохранена для рассмотрения. Ответ придёт сюда после обработки владельцем.', reply_markup=back_button())
    for owner in config.owner_ids:
        try:
            await deliver_private_report(bot, owner, row, reporter)
        except TelegramAPIError:
            log.warning('Report %s notification failed; available in owner inbox', row.id, exc_info=True)


@router.callback_query(F.data == 'admin:private_reports')
async def inbox(callback: CallbackQuery, session: AsyncSession, config: Settings):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    rows = (await session.scalars(select(PrivateReport).where(PrivateReport.status == 'open').order_by(PrivateReport.id).limit(20))).all()
    await callback.answer()
    buttons = [[InlineKeyboardButton(text=f'#{row.id} · @{row.target_username}', callback_data=f'private_report:view:{row.id}')] for row in rows]
    buttons.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')])
    await callback.message.answer('🚨 Открытые личные жалобы (первые 20). После обработки списка появятся следующие.' if rows else 'Открытых личных жалоб нет.', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith('private_report:view:'))
async def view(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    row = await session.get(PrivateReport, int(callback.data.rsplit(':', 1)[1]))
    await callback.answer()
    if row:
        reporter = await session.get(User, row.reporter_user_id) if row.reporter_user_id else None
        await deliver_private_report(bot, callback.from_user.id, row, reporter)


@router.callback_query(F.data.startswith('private_report:review:') | F.data.startswith('private_report:close:'))
async def review(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings):
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True)
        return
    _, action, raw_id = callback.data.split(':')
    status = 'reviewed' if action == 'review' else 'closed'
    result = await session.execute(update(PrivateReport).where(PrivateReport.id == int(raw_id), PrivateReport.status == 'open').values(status=status, reviewed_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        await callback.answer('Уже обработано', show_alert=True)
        return
    row = await session.get(PrivateReport, int(raw_id))
    await session.commit()
    await callback.answer('Статус обновлён')
    await callback.message.edit_reply_markup(reply_markup=back_button('admin:home'))
    reporter = await session.get(User, row.reporter_user_id) if row.reporter_user_id else None
    if reporter:
        try:
            await bot.send_message(reporter.telegram_id, f"🚨 Жалоба #{row.id}: {'рассмотрена' if status == 'reviewed' else 'закрыта'}. Решение о санкциях принимается отдельно.", reply_markup=back_button())
        except TelegramAPIError:
            log.info('Could not notify complainant for report %s', row.id)


@router.message(PrivateReportForm.target, ~F.text)
@router.message(PrivateReportForm.reason, ~F.text)
@router.message(PrivateReportForm.media)
async def invalid_input(message: Message):
    await message.answer('Отправьте данные запрошенного типа. На шаге доказательств подходят фото и видео; /cancel завершает их загрузку.', reply_markup=back_button())
