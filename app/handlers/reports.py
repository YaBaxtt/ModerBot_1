from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatModerator, PrivateReport, Report, ReportStatus, User
from app.services.text import user_label
from app.services.users import upsert_chat, upsert_user
from app.keyboards.common import back_button
from app.services.moderators import active_assignment

router = Router(name="reports")
log = logging.getLogger(__name__)


async def private_feedback(message, bot, text):
    try:
        await bot.send_message(message.chat.id, text, ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=message.from_user.id))
    except TelegramAPIError:
        try:
            await bot.send_message(message.from_user.id, text, reply_markup=back_button())
        except TelegramAPIError:
            log.info('Private report feedback unavailable')


async def report_moderators(session: AsyncSession, chat_id: int) -> list[User]:
    """Return only explicitly appointed, active moderators of this chat."""
    return list((await session.scalars(
        select(User)
        .join(ChatModerator, ChatModerator.user_id == User.id)
        .where(ChatModerator.chat_id == chat_id, ChatModerator.is_active.is_(True))
        .order_by(ChatModerator.granted_at)
    )).all())


@router.message(Command("report"), F.chat.type.in_({"group", "supergroup"}))
async def report(message: Message, bot: Bot, session: AsyncSession, config: Settings, group_command_deleted: bool = False) -> None:
    # Do this before validation and database work to minimize public exposure.
    if not group_command_deleted:
        try:
            if message.ephemeral_message_id:
                await bot.delete_ephemeral_message(message.chat.id, message.from_user.id, message.ephemeral_message_id)
            else:
                await message.delete()
        except TelegramAPIError:
            await private_feedback(message, bot, '⚠️ Не удалось удалить команду. Удалите её вручную; для приватного обращения используйте /report @username в личке бота.')
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await private_feedback(message, bot, "🚨 Ответьте командой /report на сообщение нарушителя. Пример: <code>/report флуд</code>")
        return
    if message.reply_to_message.from_user.id == message.from_user.id:
        await private_feedback(message, bot, "Нельзя отправить жалобу на самого себя.")
        return
    if message.reply_to_message.from_user.id in config.owner_ids:
        await private_feedback(message, bot, 'На владельцев бота отправлять жалобы нельзя.')
        return
    try:
        target_member = await bot.get_chat_member(message.chat.id, message.reply_to_message.from_user.id)
        if target_member.status in {'administrator', 'creator'}:
            await private_feedback(message, bot, 'На администраторов этой группы отправлять жалобы нельзя.')
            return
    except Exception:
        # A missing lookup must not discard an otherwise valid report.
        pass
    known_chat = await session.scalar(select(Chat).where(Chat.telegram_id == message.chat.id))
    known_target = await session.scalar(select(User).where(User.telegram_id == message.reply_to_message.from_user.id))
    if known_chat and known_target and await session.scalar(select(ChatModerator.id).where(ChatModerator.chat_id == known_chat.id, ChatModerator.user_id == known_target.id, ChatModerator.is_active.is_(True))):
        await private_feedback(message, bot, 'На назначенных модераторов этой группы отправлять жалобы нельзя.')
        return
    reason = " ".join(message.text.split()[1:]).strip()
    if not reason or len(reason) > 1500:
        await private_feedback(message, bot, "Укажите причину до 1500 символов: <code>/report флуд</code>")
        return
    reporter, target, chat = await upsert_user(session, message.from_user), await upsert_user(session, message.reply_to_message.from_user), await upsert_chat(session, message.chat)
    item = Report(chat_id=chat.id, reporter_user_id=reporter.id, target_user_id=target.id, telegram_message_id=message.reply_to_message.message_id, reason=reason)
    session.add(item); await session.commit()
    link = f"https://t.me/{message.chat.username}/{message.reply_to_message.message_id}" if message.chat.username else "—"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Рассмотрено", callback_data=f"report:review:{item.id}", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="❌ Закрыть", callback_data=f"report:close:{item.id}"),
    ], [InlineKeyboardButton(text='🛡 В модераторскую', callback_data=f'moder:chat:{chat.id}')]])
    text = f"🚨 <b>Новая жалоба #{item.id}</b>\n\nОт кого: {user_label(message.from_user)}\nНа кого: {user_label(message.reply_to_message.from_user)}\nЧат: {escape(chat.title)}\nПричина: {escape(reason)}\nСообщение: {link}"
    delivered = 0
    moderators = await report_moderators(session, chat.id)
    for moderator in moderators:
        try:
            await bot.send_message(moderator.telegram_id, text, reply_markup=markup)
            delivered += 1
        except TelegramAPIError:
            # Telegram forbids a bot from initiating a private conversation.
            # The assignment stays valid; the moderator only needs to press
            # /start once before future reports can be delivered.
            log.warning('Could not notify moderator %s about report %s', moderator.telegram_id, item.id)
    if delivered:
        result = f'✅ Жалоба #{item.id} сохранена и отправлена модераторам: <b>{delivered}</b>. В общий чат подтверждение не отправляется.'
    elif moderators:
        result = f'⚠️ Жалоба #{item.id} сохранена, но Telegram не разрешил доставить её модераторам в личку. Модераторам нужно один раз открыть бота и нажать /start.'
    else:
        result = f'⚠️ Жалоба #{item.id} сохранена, но у этой группы пока нет назначенных модераторов. Владелец группы может добавить их в разделе «Модераторская».'
    await private_feedback(message, bot, result)


@router.callback_query(F.data.startswith("report:review:") | F.data.startswith("report:close:"))
async def review_report(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    _, status, raw_id = callback.data.split(":")
    report_row = await session.get(Report, int(raw_id), with_for_update=True)
    if not report_row or report_row.status != ReportStatus.OPEN: await callback.answer("Уже обработано", show_alert=True); return
    if not config.is_owner(callback.from_user.id) and not await active_assignment(session, report_row.chat_id, callback.from_user.id):
        await callback.answer('Эту жалобу может обработать только назначенный модератор этой группы.', show_alert=True)
        return
    report_row.status = ReportStatus.REVIEWED if status == "review" else ReportStatus.CLOSED
    report_row.reviewed_at = datetime.now(timezone.utc)
    reviewer = await upsert_user(session, callback.from_user)
    report_row.reviewed_by_user_id = reviewer.id
    await callback.answer("Статус обновлён")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == 'admin:reports')
async def reports_inbox(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    group_rows = (await session.scalars(select(Report).where(Report.status == ReportStatus.OPEN).order_by(Report.id).limit(10))).all()
    private_rows = (await session.scalars(select(PrivateReport).where(PrivateReport.status == 'open').order_by(PrivateReport.id).limit(10))).all()
    rows = [[InlineKeyboardButton(text=f'🚩 Группа · #{row.id}', callback_data=f'report:view:{row.id}')] for row in group_rows]
    rows += [[InlineKeyboardButton(text=f'🕵️ Личная · #{row.id} · @{row.target_username}', callback_data=f'private_report:view:{row.id}')] for row in private_rows]
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')])
    await callback.answer()
    await callback.message.edit_text(f'🚨 <b>ЦЕНТР ЖАЛОБ</b>\n━━━━━━━━━━━━\n\n🚩 Из групп: <b>{len(group_rows)}</b>\n🕵️ Из лички: <b>{len(private_rows)}</b>\n\nВыберите обращение.' if group_rows or private_rows else '🚨 <b>ЦЕНТР ЖАЛОБ</b>\n━━━━━━━━━━━━\n\n🟢 Открытых жалоб нет.', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('report:view:'))
async def view_group_report(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Нет доступа', show_alert=True); return
    row = await session.get(Report, int(callback.data.rsplit(':', 1)[1]))
    if not row:
        await callback.answer('Жалоба не найдена', show_alert=True); return
    reporter = await session.get(User, row.reporter_user_id) if row.reporter_user_id else None
    target = await session.get(User, row.target_user_id) if row.target_user_id else None
    chat = await session.get(Chat, row.chat_id)
    link = f'https://t.me/{chat.username}/{row.telegram_message_id}' if chat and chat.username and row.telegram_message_id else '—'
    text = f'🚩 <b>Жалоба из группы #{row.id}</b>\n\nОт кого: {user_label(reporter) if reporter else "пользователь удалён"}\nНа кого: {user_label(target) if target else "пользователь удалён"}\nЧат: {escape(chat.title) if chat else "удалён"}\nПричина: {escape(row.reason)}\nСообщение: {link}'
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Рассмотрено', callback_data=f'report:review:{row.id}'), InlineKeyboardButton(text='❌ Закрыть', callback_data=f'report:close:{row.id}')], [InlineKeyboardButton(text='⬅️ К жалобам', callback_data='admin:reports')]]) if row.status == ReportStatus.OPEN else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ К жалобам', callback_data='admin:reports')]])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)
