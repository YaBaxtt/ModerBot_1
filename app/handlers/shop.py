from __future__ import annotations

from html import escape
import logging
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import PurchaseStatus, ShopItem, ShopPurchase, User
from app.services.shop import PurchaseError, create_purchase, refund_purchase
from app.services.users import upsert_user
from app.services.text import user_label, user_profile_url

router = Router(name="shop")
log = logging.getLogger(__name__)


async def shop_text(session: AsyncSession, balance: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    items = (await session.scalars(select(ShopItem).where(ShopItem.is_active.is_(True)).order_by(ShopItem.id))).all()
    rows = [[InlineKeyboardButton(text=f"{item.name[:48]} · {item.price} ⭐", callback_data=f"shop:item:{item.id}")] for item in items]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:private_main")])
    balance_line = f"\n⭐ Ваш баланс: <b>{balance:,}</b>" if balance is not None else ""
    header = f"🛍 <b>МАГАЗИН СООБЩЕСТВА</b>\n━━━━━━━━━━━━{balance_line}\n📦 Доступно товаров: <b>{len(items)}</b>"
    hint = "<blockquote>Выберите товар — перед списанием очков бот обязательно попросит подтверждение.</blockquote>" if items else "<blockquote>Сейчас витрина пуста. Новые товары появятся здесь после добавления владельцем.</blockquote>"
    return (f'{header}\n\n{hint}', InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("shop"), F.chat.type == "private")
async def shop_command(message: Message, session: AsyncSession) -> None:
    user = await upsert_user(session, message.from_user)
    text, markup = await shop_text(session, user.points); await message.answer(text, reply_markup=markup)


@router.message(Command("shop"), F.chat.type.in_({"group", "supergroup"}))
async def group_shop_command(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 Открыть магазин", url=f"https://t.me/{me.username}?start=shop")]]) if me.username else None
    await message.answer("🛍 Магазин доступен в личных сообщениях бота.", reply_markup=markup)


@router.callback_query(F.data == "shop:list", F.message.chat.type == "private")
async def list_shop(callback: CallbackQuery, session: AsyncSession) -> None:
    user = await upsert_user(session, callback.from_user)
    await callback.answer(); text, markup = await shop_text(session, user.points); await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("shop:item:"), F.message.chat.type == "private")
async def item_detail(callback: CallbackQuery, session: AsyncSession) -> None:
    item = await session.get(ShopItem, int(callback.data.rsplit(":", 1)[1]))
    await callback.answer()
    if not item or not item.is_active: await callback.message.edit_text("Товар больше недоступен."); return
    description = f'\n\n<i>{escape(item.description)}</i>' if item.description else ''
    stock = 'без ограничения' if item.stock is None else str(item.stock)
    await callback.message.edit_text(f"🛍 <b>{escape(item.name)}</b>\n━━━━━━━━━━━━{description}\n\n⭐ Цена: <b>{item.price:,}</b>\n📦 В наличии: <b>{stock}</b>\n\n<blockquote>Подтвердите покупку. После списания очков владелец выдаст товар или Telegram-подарок в течение 24 часов.</blockquote>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Купить", callback_data=f"shop:buy:{item.id}", style=ButtonStyle.SUCCESS), InlineKeyboardButton(text="❌ Отмена", callback_data="shop:list")]]))


@router.callback_query(F.data.startswith("shop:buy:"), F.message.chat.type == "private")
async def buy(callback: CallbackQuery, bot: Bot, session: AsyncSession, config: Settings) -> None:
    buyer = await upsert_user(session, callback.from_user)
    try: purchase = await create_purchase(session, buyer_id=buyer.id, item_id=int(callback.data.rsplit(":", 1)[1]), request_key=f"{callback.message.chat.id}:{callback.message.message_id}:{callback.data}")
    except PurchaseError as exc: await callback.answer(str(exc), show_alert=True); return
    item = await session.get(ShopItem, purchase.item_id)
    await session.commit()
    await callback.answer("Покупка оформлена!")
    await callback.message.edit_text(f"✅ <b>ПОКУПКА #{purchase.id} ОФОРМЛЕНА</b>\n━━━━━━━━━━━━\n\n⭐ Списано: <b>{purchase.price_paid:,}</b>\n🎁 Товар: <b>{escape(item.name) if item else 'удалён'}</b>\n\n<blockquote>Владелец получит ссылку на ваш профиль и выдаст товар или Telegram-подарок в течение 24 часов.</blockquote>")
    profile_url = user_profile_url(callback.from_user)
    action_rows = []
    if profile_url:
        action_rows.append([InlineKeyboardButton(text='👤 Открыть покупателя', url=profile_url)])
    action_rows.extend([[
        InlineKeyboardButton(text="✅ Выдано", callback_data=f"purchase:fulfill:{purchase.id}", style=ButtonStyle.SUCCESS), InlineKeyboardButton(text="↩️ Возврат", callback_data=f"purchase:refund:{purchase.id}")
    ], [InlineKeyboardButton(text='⬅️ В меню', callback_data='admin:home')]])
    actions = InlineKeyboardMarkup(inline_keyboard=action_rows)
    for owner_id in config.owner_ids:
        try: await bot.send_message(owner_id, f"🧾 <b>НОВАЯ ПОКУПКА #{purchase.id}</b>\n━━━━━━━━━━━━\n\n👤 Покупатель: {user_label(callback.from_user)}\n🎁 Товар: <b>{escape(item.name) if item else 'удалён'}</b>\n⭐ Цена: <b>{purchase.price_paid:,}</b>\n\n<blockquote>Нажмите «Открыть покупателя», чтобы перейти в профиль и выдать товар или Telegram-подарок.</blockquote>", reply_markup=actions)
        except TelegramAPIError:
            log.warning('Could not notify owner %s about purchase %s', owner_id, purchase.id, exc_info=True)


@router.callback_query(F.data.startswith("purchase:"))
async def purchase_action(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if not config.is_owner(callback.from_user.id): await callback.answer("Нет доступа", show_alert=True); return
    _, action, raw_id = callback.data.split(":")
    purchase = await session.get(ShopPurchase, int(raw_id), with_for_update=True)
    if not purchase or purchase.status != PurchaseStatus.PENDING: await callback.answer("Уже обработано", show_alert=True); return
    if action == "fulfill":
        result = await session.execute(update(ShopPurchase).where(ShopPurchase.id == purchase.id, ShopPurchase.status == PurchaseStatus.PENDING).values(status=PurchaseStatus.FULFILLED))
        if result.rowcount != 1:
            await callback.answer("Уже обработано", show_alert=True)
            return
        text = "✅ Покупка отмечена как выданная."
    elif action == "refund":
        purchase = await refund_purchase(session, purchase.id)
        if not purchase:
            await callback.answer("Уже обработано", show_alert=True)
            return
        text = "↩️ Очки возвращены покупателю."
    else:
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    await session.commit()
    await callback.answer(text); await callback.message.edit_reply_markup(reply_markup=None)
