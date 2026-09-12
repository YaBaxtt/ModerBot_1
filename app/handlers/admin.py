from __future__ import annotations

from datetime import date, timedelta
from html import escape
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import AdvertisingRequest, AdvertisingStatus, Broadcast, Chat, DailyActivity, JoinVerification, MemberProfileSnapshot, PrivateReport, PurchaseStatus, Report, ReportStatus, Setting, ShopItem, ShopPurchase, User
from app.handlers.common import DEFAULT_RULES
from app.keyboards.common import admin_menu
from app.services.health import inspect_chat
from app.services.parse import normalise_telegram_url
from app.services.features import FEATURES, FEATURE_BY_KEY, feature_states, set_feature
from app.handlers.verification import release_pending_verifications
from app.services.broadcasts import sync_broadcast_jobs

router = Router(name="admin")


class RulesForm(StatesGroup):
    text = State()


class ShopCreate(StatesGroup):
    name = State()
    price = State()


class MenuLinkForm(StatesGroup):
    url = State()


async def deny_if_not_owner(message: Message | CallbackQuery, config: Settings) -> bool:
    if config.is_owner(message.from_user.id): return False
    if isinstance(message, CallbackQuery): await message.answer("Нет доступа", show_alert=True)
    else: await message.answer("Эта команда доступна только владельцам.")
    return True


COMMANDS = """📋 <b>Команды бота</b>

<b>Для пользователей</b>
/start — открыть меню
/help — помощь
/me, /profile — профиль и очки
/daily — ежедневная награда и серия дней
/top — рейтинг активности
/shop — магазин за очки
/rules — правила сообщества
/report причина — жалоба (ответом на сообщение в группе)
/report @username — личная жалоба с фото или видео (в личке бота)
/moder — свои группы, доверенные модераторы и их статистика
/cancel — отменить текущее заполнение формы

<b>Для администраторов группы</b>
/ban причина — заблокировать
/unban — снять бан
/mute 1h причина — ограничить отправку сообщений
/unmute — снять мут
/warn причина — выдать предупреждение
/unwarn — снять последнее предупреждение
/history — последние действия модерации

Команды модерации отправляйте <b>ответом на сообщение участника</b>.
Можно указать известный боту @username или ID.
Время мута: <code>10m</code>, <code>1h</code>, <code>1d</code>.

<b>Для владельцев бота</b>
/admin — панель управления в личных сообщениях
Товары, рассылки, правила и обязательная подписка настраиваются кнопками панели."""


@router.callback_query(F.data == "admin:commands")
async def commands(callback: CallbackQuery, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    await callback.answer()
    await callback.message.edit_text(COMMANDS, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]]))


@router.callback_query(F.data == "admin:health")
async def health(callback: CallbackQuery, bot: Bot, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    await callback.answer("Проверяю права…")
    rows = (await session.scalars(select(Chat).where(Chat.is_active.is_(True)).order_by(Chat.id))).all()
    # Each message is bounded even when many groups are connected.
    await callback.message.answer("🩺 <b>Проверка бота</b>\nХранилище доступно. Проверяю фактические права в чатах.")
    for chat in rows:
        await callback.message.answer(await inspect_chat(bot, chat))
    await callback.message.answer(
        "Проверка завершена. Зелёные отметки означают наличие нужных прав; доставку сообщений проверьте в самом чате." if rows else "Пока нет сохранённых активных чатов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Проверить снова", callback_data="admin:health"),
            InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]]))


@router.message(Command("admin"), F.chat.type == "private")
async def admin_command(message: Message, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    await message.answer("👑 <b>СУПЕР-АДМИНКА</b>\n━━━━━━━━━━━━\n\nГлобальное управление всем ботом. Настройки отдельных сообществ находятся в разделе «Настройки группы».", reply_markup=admin_menu())


@router.message(Command("admin"), F.chat.type.in_({"group", "supergroup"}))
async def group_admin_command(message: Message, bot: Bot, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    me = await bot.get_me()
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Открыть панель", url=f"https://t.me/{me.username}")]]) if me.username else None
    await message.answer("👑 Супер-админка доступна владельцу в личных сообщениях бота.", reply_markup=markup)


@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    await callback.answer()
    if callback.message.text:
        await callback.message.edit_text("👑 <b>СУПЕР-АДМИНКА</b>\n━━━━━━━━━━━━\n\nГлобальное управление всем ботом. Настройки отдельных сообществ находятся в разделе «Настройки группы».", reply_markup=admin_menu())
    else:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("👑 <b>СУПЕР-АДМИНКА</b>\n━━━━━━━━━━━━\n\nГлобальное управление всем ботом. Настройки отдельных сообществ находятся в разделе «Настройки группы».", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:stats")
async def stats(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    today = date.today()
    users = await session.scalar(select(func.count(User.id))) or 0
    chats = await session.scalar(select(func.count(Chat.id)).where(Chat.is_active.is_(True))) or 0
    messages = await session.scalar(select(func.coalesce(func.sum(User.message_count), 0))) or 0
    points = await session.scalar(select(func.coalesce(func.sum(User.points), 0))) or 0
    active_today = await session.scalar(select(func.count(func.distinct(DailyActivity.user_id))).where(DailyActivity.day == today)) or 0
    today_messages = await session.scalar(select(func.coalesce(func.sum(DailyActivity.message_count), 0)).where(DailyActivity.day == today)) or 0
    open_reports = (await session.scalar(select(func.count(Report.id)).where(Report.status == ReportStatus.OPEN)) or 0) + (await session.scalar(select(func.count(PrivateReport.id)).where(PrivateReport.status == 'open')) or 0)
    pending_ads = await session.scalar(select(func.count(AdvertisingRequest.id)).where(AdvertisingRequest.status == AdvertisingStatus.NEW)) or 0
    pending_purchases = await session.scalar(select(func.count(ShopPurchase.id)).where(ShopPurchase.status == PurchaseStatus.PENDING)) or 0
    activity_rows = dict((await session.execute(select(DailyActivity.day, func.sum(DailyActivity.message_count)).where(DailyActivity.day >= today - timedelta(days=6)).group_by(DailyActivity.day))).all())
    maximum = max(activity_rows.values(), default=0) or 1
    chart = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        value = int(activity_rows.get(day, 0))
        filled = round(value / maximum * 8) if value else 0
        chart.append(f'<code>{day.strftime("%d.%m")} {"▰" * filled}{"▱" * (8 - filled)} {value}</code>')
    text = f'''📊 <b>ПАНЕЛЬ СТАТИСТИКИ</b>
━━━━━━━━━━━━

<b>СООБЩЕСТВО</b>
👥 Пользователей: <b>{users:,}</b>
💬 Подключённых чатов: <b>{chats:,}</b>
💭 Сообщений за всё время: <b>{messages:,}</b>
⭐ Очков на балансах: <b>{points:,}</b>

<b>СЕГОДНЯ</b>
🟢 Активных участников: <b>{active_today:,}</b>
✉️ Сообщений: <b>{today_messages:,}</b>

<b>ТРЕБУЕТ ВНИМАНИЯ</b>
🚨 Открытых жалоб: <b>{open_reports:,}</b>
💼 Новых рекламных заявок: <b>{pending_ads:,}</b>
🛍 Покупок к выдаче: <b>{pending_purchases:,}</b>

<b>АКТИВНОСТЬ ЗА 7 ДНЕЙ</b>
{chr(10).join(chart)}'''
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats"), InlineKeyboardButton(text="🚨 Центр внимания", callback_data="admin:attention")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]])
    await callback.answer(); await callback.message.edit_text(text, reply_markup=markup)


async def render_feature_panel(message: Message, session: AsyncSession, note: str | None = None) -> None:
    states = await feature_states(session)
    enabled_count = sum(states.values())
    rows = [[InlineKeyboardButton(text=f'{"✅" if states[feature.key] else "❌"} {feature.title}', callback_data=f'admin:feature:{feature.key}')]
            for feature in FEATURES]
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')])
    text = f'🧩 <b>УПРАВЛЕНИЕ ФУНКЦИЯМИ</b>\n━━━━━━━━━━━━\n\nРаботает: <b>{enabled_count}/{len(FEATURES)}</b>\nНажмите функцию, чтобы включить или отключить её. Изменение сохраняется после перезапуска.'
    if note: text += f'\n\n{note}'
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == 'admin:features')
async def features_panel(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    await callback.answer()
    await render_feature_panel(callback.message, session)


@router.callback_query(F.data.startswith('admin:feature:'))
async def toggle_feature(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings, session_factory) -> None:
    if await deny_if_not_owner(callback, config): return
    key = callback.data.rsplit(':', 1)[1]
    if key not in FEATURE_BY_KEY:
        await callback.answer('Неизвестная функция', show_alert=True); return
    current = (await feature_states(session, (key,)))[key]
    enabled = not current
    await set_feature(session, key, enabled)
    await session.commit()
    note = f'{"✅ Включено" if enabled else "❌ Отключено"}: <b>{FEATURE_BY_KEY[key].title}</b> — {FEATURE_BY_KEY[key].description}.'
    if key == 'verification' and not enabled:
        released, failed = await release_pending_verifications(bot, session)
        note += f'\n🔓 Ожидавших тест: освобождено {released}, ошибок {failed}.'
    if key == 'broadcasts':
        jobs = await sync_broadcast_jobs(bot, session_factory, enabled)
        note += f'\n📅 Заданий рассылки {"возобновлено" if enabled else "приостановлено"}: {jobs}.'
    if key == 'member_tracking' and enabled:
        await session.execute(delete(MemberProfileSnapshot))
        await session.commit()
        note += '\n🧹 Снимки профилей сброшены: первое новое действие каждого участника сохранит базовый профиль без уведомления.'
    await callback.answer('Включено' if enabled else 'Отключено')
    await render_feature_panel(callback.message, session, note)


@router.callback_query(F.data == 'admin:attention')
async def attention_center(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    group_reports = await session.scalar(select(func.count(Report.id)).where(Report.status == ReportStatus.OPEN)) or 0
    private_reports = await session.scalar(select(func.count(PrivateReport.id)).where(PrivateReport.status == 'open')) or 0
    ads = await session.scalar(select(func.count(AdvertisingRequest.id)).where(AdvertisingRequest.status == AdvertisingStatus.NEW)) or 0
    purchases = await session.scalar(select(func.count(ShopPurchase.id)).where(ShopPurchase.status == PurchaseStatus.PENDING)) or 0
    broadcasts = await session.scalar(select(func.count(Broadcast.id)).where(Broadcast.sent_at.is_(None))) or 0
    verifications = await session.scalar(select(func.count(JoinVerification.id)).where(JoinVerification.is_verified.is_(False), JoinVerification.completed_at.is_(None))) or 0
    total = group_reports + private_reports + ads + purchases
    text = f'''🚨 <b>ЦЕНТР ВНИМАНИЯ</b>
━━━━━━━━━━━━

{("🟢 Срочных задач нет." if total == 0 else f"🔴 Требуют решения: <b>{total}</b>")}

🚩 Жалобы из групп: <b>{group_reports}</b>
🕵️ Личные жалобы: <b>{private_reports}</b>
💼 Рекламные заявки: <b>{ads}</b>
🎁 Покупки к выдаче: <b>{purchases}</b>

⏳ Запланированные рассылки: <b>{broadcasts}</b>
🧩 Ожидают тест новичка: <b>{verifications}</b>'''
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🚨 Открыть жалобы', callback_data='admin:reports'), InlineKeyboardButton(text='🩺 Проверить бота', callback_data='admin:health')], [InlineKeyboardButton(text='🔄 Обновить', callback_data='admin:attention'), InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')]])
    await callback.answer(); await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:chats")
async def chats(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    rows = (await session.scalars(select(Chat).where(Chat.is_active.is_(True)).order_by(Chat.created_at.desc()))).all()
    await callback.answer(); await callback.message.edit_text("💬 <b>Подключённые чаты</b>\n\n" + ("\n".join(f"• {escape(c.title)} — <code>{c.telegram_id}</code>" for c in rows) if rows else "Чатов пока нет."), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]]))


@router.callback_query(F.data == 'admin:links')
async def menu_link_settings(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    group = await session.get(Setting, 'community_group_url')
    channel = await session.get(Setting, 'community_channel_url')
    group_url = group.value if group else getattr(config, 'community_group_url', None)
    channel_url = channel.value if channel else getattr(config, 'community_channel_url', None)
    text = f'🔗 <b>Ссылки главного меню</b>\n\n👥 Группа: {escape(group_url) if group_url else "не настроена"}\n📣 Канал: {escape(channel_url) if channel_url else "не настроен"}\n\nНажмите нужную кнопку и отправьте ссылку вида <code>https://t.me/name</code>.'
    rows = [
        [InlineKeyboardButton(text='👥 Задать группу', callback_data='admin:link:group'), InlineKeyboardButton(text='📣 Задать канал', callback_data='admin:link:channel')],
    ]
    if group or channel:
        delete_row = []
        if group: delete_row.append(InlineKeyboardButton(text='🗑 Группу', callback_data='admin:link_delete:group'))
        if channel: delete_row.append(InlineKeyboardButton(text='🗑 Канал', callback_data='admin:link_delete:channel'))
        rows.append(delete_row)
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home')])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('admin:link:'))
async def menu_link_start(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    kind = callback.data.rsplit(':', 1)[1]
    if kind not in {'group', 'channel'}:
        await callback.answer('Неизвестный тип ссылки', show_alert=True); return
    await state.set_state(MenuLinkForm.url)
    await state.update_data(menu_link_kind=kind)
    await callback.answer()
    await callback.message.answer(f'Отправьте Telegram-ссылку на {"группу" if kind == "group" else "канал"}.\nПример: <code>https://t.me/name</code>', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:links')]]))


@router.message(MenuLinkForm.url, F.text)
async def menu_link_save(message: Message, state: FSMContext, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    url = normalise_telegram_url(message.text)
    if not url:
        await message.answer('Это не Telegram-ссылка. Отправьте ссылку вида <code>https://t.me/name</code>.')
        return
    kind = (await state.get_data()).get('menu_link_kind')
    if kind not in {'group', 'channel'}:
        await state.clear(); await message.answer('Настройка устарела. Откройте раздел ссылок заново.', reply_markup=admin_menu()); return
    key = f'community_{kind}_url'
    row = await session.get(Setting, key)
    if row: row.value = url
    else: session.add(Setting(key=key, value=url))
    await session.commit()
    await state.clear()
    await message.answer(f'✅ Ссылка на {"группу" if kind == "group" else "канал"} сохранена. Новая кнопка уже работает.', reply_markup=admin_menu())


@router.callback_query(F.data.startswith('admin:link_delete:'))
async def menu_link_delete(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    kind = callback.data.rsplit(':', 1)[1]
    if kind not in {'group', 'channel'}:
        await callback.answer('Неизвестный тип ссылки', show_alert=True); return
    row = await session.get(Setting, f'community_{kind}_url')
    if row:
        await session.delete(row)
        await session.commit()
    await menu_link_settings(callback, session, config)


@router.callback_query(F.data == "admin:rules")
async def rules_settings(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    current = await session.scalar(select(Setting.value).where(Setting.key == "rules")) or DEFAULT_RULES
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить", callback_data="admin:rules_edit"), InlineKeyboardButton(text="↩️ Сбросить", callback_data="admin:rules_reset")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")]])
    await callback.answer(); await callback.message.edit_text(f"⚙️ <b>Правила сообщества</b>\n\n{escape(current)}", reply_markup=markup)


@router.callback_query(F.data == "admin:rules_edit")
async def rules_edit(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    await callback.answer(); await state.set_state(RulesForm.text)
    await callback.message.answer("Отправьте новый текст правил. Для отмены нажмите /cancel.")


@router.message(RulesForm.text, F.text)
async def rules_save(message: Message, state: FSMContext, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    row = await session.get(Setting, "rules")
    if row: row.value = message.text[:4000]
    else: session.add(Setting(key="rules", value=message.text[:4000]))
    await state.clear(); await message.answer("✅ Правила сохранены.", reply_markup=admin_menu())


@router.message(Command("cancel"), RulesForm.text)
async def rules_cancel(message: Message, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    await state.clear()
    await message.answer("Изменение правил отменено.", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:shop_add")
async def shop_add_start(callback: CallbackQuery, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(callback, config):
        return
    await callback.answer()
    await state.set_state(ShopCreate.name)
    await callback.message.answer("🛍 Введите название товара.\n\nДля отмены: /cancel")


@router.message(ShopCreate.name, F.text)
async def shop_add_name(message: Message, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(message, config): return
    name = message.text.strip()
    if not name or len(name) > 255:
        await message.answer("Название должно содержать от 1 до 255 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(ShopCreate.price)
    await message.answer("Введите цену товара в очках.")


@router.message(ShopCreate.price, F.text)
async def shop_add_price(message: Message, state: FSMContext, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(message, config):
        return
    try:
        price = int(message.text.strip())
    except ValueError:
        price = 0
    if price < 1:
        await message.answer("Цена должна быть целым числом больше нуля.")
        return
    data = await state.get_data()
    item = ShopItem(name=data["name"], price=price, is_active=True)
    session.add(item)
    await session.flush()
    await state.clear()
    await message.answer(f"✅ Товар «{escape(item.name)}» добавлен за {item.price} ⭐.", reply_markup=admin_menu())


@router.message(Command("cancel"), ShopCreate.name)
@router.message(Command("cancel"), ShopCreate.price)
async def shop_add_cancel(message: Message, state: FSMContext, config: Settings) -> None:
    if await deny_if_not_owner(message, config):
        return
    await state.clear()
    await message.answer("Добавление товара отменено.", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:rules_reset")
async def rules_reset(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if await deny_if_not_owner(callback, config): return
    row = await session.get(Setting, "rules")
    if row: row.value = DEFAULT_RULES
    else: session.add(Setting(key="rules", value=DEFAULT_RULES))
    await callback.answer("Правила сброшены")
    await rules_settings(callback, session, config)
