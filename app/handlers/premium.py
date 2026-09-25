from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatPremiumAccess
from app.keyboards.common import back_button
from app.services.moderators import can_manage_chat, can_moderate_chat
from app.services.premium import build_payload, chat_has_pro, parse_payload
from app.services.users import upsert_user


router = Router(name='premium')


async def purchasable_chat(session: AsyncSession, bot: Bot, config: Settings, chat_id: int, buyer_id: int) -> Chat | None:
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.is_active or not await can_manage_chat(bot, chat, buyer_id, config):
        return None
    return chat


async def accessible_chat(session: AsyncSession, bot: Bot, config: Settings, chat_id: int, user_id: int) -> Chat | None:
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.is_active or not await can_moderate_chat(bot, session, chat, user_id, config):
        return None
    return chat


@router.callback_query(F.data.startswith('premium:info:'))
async def premium_info(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await accessible_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat:
        await callback.answer('У вас нет доступа к этой группе.', show_alert=True)
        return
    can_buy = await can_manage_chat(bot, chat, callback.from_user.id, config)
    active = await chat_has_pro(session, chat.id, callback.from_user.id, config)
    if active:
        text = f'💎 <b>PRO УЖЕ АКТИВЕН</b>\n━━━━━━━━━━━━\n\nГруппа: <b>{escape(chat.title)}</b>\nСрок: <b>навсегда</b>\n\nВсе текущие и будущие PRO-функции этой группы доступны без доплат.'
        markup = back_button(f'menu:group_settings:{chat.id}')
    else:
        text = f'''💎 <b>NIK MODER BOT PRO</b>
━━━━━━━━━━━━

Группа: <b>{escape(chat.title)}</b>
Цена: <b>{config.premium_price_stars} Telegram Stars</b>
Срок: <b>навсегда</b>

<blockquote>🎞 Фильтрация медиа
🔤 Пользовательский список запрещённых слов
🔞 Фильтр 18+ слов и ссылок
🛡 Защита от 4 входов за 2 секунды
🤖 Автобан добавляемых ботов
👻 Контроль сообщений от имени каналов
⚙️ Все будущие PRO-функции без доплаты</blockquote>

Покупка закрепляется за этой группой и не является подпиской.'''
        rows = []
        if can_buy:
            rows.append([InlineKeyboardButton(text=f'⭐ Купить навсегда · {config.premium_price_stars} XTR', callback_data=f'premium:buy:{chat.id}', style=ButtonStyle.SUCCESS)])
        else:
            text += '\n\n<i>Активировать PRO может создатель группы.</i>'
        rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data=f'menu:group_settings:{chat.id}')])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith('premium:buy:'))
async def buy_premium(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа', show_alert=True)
        return
    chat = await purchasable_chat(session, bot, config, chat_id, callback.from_user.id)
    if not chat:
        await callback.answer('Покупка доступна только создателю этой группы.', show_alert=True)
        return
    if await chat_has_pro(session, chat.id, callback.from_user.id, config):
        await callback.answer('PRO уже активен для этой группы.', show_alert=True)
        return
    payload = build_payload(chat.id, callback.from_user.id, config.premium_price_stars)
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title='Nik Moder Bot PRO',
        description=f'Пожизненный PRO-доступ для группы «{chat.title[:80]}»',
        payload=payload,
        currency='XTR',
        prices=[LabeledPrice(label='PRO навсегда', amount=config.premium_price_stars)],
    )


@router.pre_checkout_query()
async def premium_pre_checkout(query: PreCheckoutQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    parsed = parse_payload(query.invoice_payload)
    if not parsed or query.currency != 'XTR':
        await query.answer(ok=False, error_message='Некорректный счёт. Откройте настройки группы заново.')
        return
    chat_id, buyer_id, price = parsed
    chat = await purchasable_chat(session, bot, config, chat_id, query.from_user.id)
    valid = bool(chat and buyer_id == query.from_user.id and price == query.total_amount == config.premium_price_stars)
    if not valid:
        await query.answer(ok=False, error_message='Цена или владелец группы изменились. Создайте новый счёт.')
        return
    if await session.scalar(select(ChatPremiumAccess.id).where(ChatPremiumAccess.chat_id == chat_id)):
        await query.answer(ok=False, error_message='PRO для этой группы уже активирован.')
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def premium_success(message: Message, session: AsyncSession, bot: Bot, config: Settings) -> None:
    payment = message.successful_payment
    parsed = parse_payload(payment.invoice_payload)
    if not parsed or payment.currency != 'XTR':
        return
    chat_id, buyer_id, price = parsed
    if buyer_id != message.from_user.id or payment.total_amount != price:
        return
    duplicate = await session.scalar(select(ChatPremiumAccess).where(ChatPremiumAccess.telegram_payment_charge_id == payment.telegram_payment_charge_id))
    if duplicate:
        await message.answer('✅ Этот платёж уже обработан.', reply_markup=back_button(f'menu:group_settings:{duplicate.chat_id}'))
        return
    # The invoice was already authorized during pre-checkout. A buyer must not
    # lose paid access merely because Telegram membership changed seconds later.
    chat = await session.get(Chat, chat_id)
    if not chat:
        try:
            await bot.refund_star_payment(user_id=message.from_user.id, telegram_payment_charge_id=payment.telegram_payment_charge_id)
            await message.answer('↩️ Группа больше не существует в боте, поэтому платёж автоматически возвращён.')
        except TelegramAPIError:
            await message.answer('Платёж получен, но группа недоступна. Обратитесь к владельцу бота и не удаляйте квитанцию.')
        return
    existing = await session.scalar(select(ChatPremiumAccess).where(ChatPremiumAccess.chat_id == chat.id))
    if existing:
        try:
            await bot.refund_star_payment(user_id=message.from_user.id, telegram_payment_charge_id=payment.telegram_payment_charge_id)
            await message.answer('↩️ PRO уже был активен. Повторный платёж автоматически возвращён.', reply_markup=back_button(f'menu:group_settings:{chat.id}'))
        except TelegramAPIError:
            await message.answer('PRO уже активен, но автоматический возврат повторного платежа не удался. Сохраните квитанцию и обратитесь к владельцу бота.', reply_markup=back_button(f'menu:group_settings:{chat.id}'))
        return
    buyer = await upsert_user(session, message.from_user)
    try:
        async with session.begin_nested():
            session.add(ChatPremiumAccess(
                chat_id=chat.id,
                purchased_by_user_id=buyer.id,
                price_stars=price,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id or None,
            ))
            await session.flush()
    except IntegrityError:
        # A second successful-payment update can race with the first one.
        # The unique constraints decide the winner without granting twice.
        processed = await session.scalar(select(ChatPremiumAccess).where(
            ChatPremiumAccess.telegram_payment_charge_id == payment.telegram_payment_charge_id,
        ))
        if processed:
            await message.answer('✅ Этот платёж уже обработан.', reply_markup=back_button(f'menu:group_settings:{processed.chat_id}'))
            return
        try:
            await bot.refund_star_payment(user_id=message.from_user.id, telegram_payment_charge_id=payment.telegram_payment_charge_id)
            await message.answer('↩️ PRO уже активирован другим платежом. Лишние Stars автоматически возвращены.', reply_markup=back_button(f'menu:group_settings:{chat.id}'))
        except TelegramAPIError:
            await message.answer('PRO уже активен, но автоматический возврат не удался. Сохраните квитанцию и обратитесь к владельцу бота.', reply_markup=back_button(f'menu:group_settings:{chat.id}'))
        return
    await message.answer(
        f'🎉 <b>PRO АКТИВИРОВАН НАВСЕГДА</b>\n━━━━━━━━━━━━\n\n💬 Группа: <b>{escape(chat.title)}</b>\n⭐ Оплачено: <b>{price}</b>\n\nВсе PRO-разделы этой группы разблокированы.',
        reply_markup=back_button(f'menu:group_settings:{chat.id}'),
    )
