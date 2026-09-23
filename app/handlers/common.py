from __future__ import annotations

from html import escape
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, EphemeralMessageParameters, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, ChatProtectionSetting, DailyReward, Setting, User, UserChatStats, Warning
from app.keyboards.common import back_button, group_help_menu, main_menu
from app.services.users import upsert_user
from app.services.profile import level_for_points, level_progress
from app.services.features import feature_states
from app.services.premium import chat_has_pro
from app.services.protections import GROUP_CONTROLS, protection_states
from app.services.moderators import can_manage_chat, chat_accesses

DEFAULT_RULES = "Соблюдайте уважение к участникам сообщества, не флудите и не публикуйте рекламу без согласования с администрацией."

router = Router(name="common")

PRIVATE_HELP = """❓ <b>Помощь</b>\n\nПрофиль — /profile или /me\nМодераторская — /moder\nЕжедневная награда — /daily\nРейтинг — /top\nМагазин — /shop\nПравила — /rules\nЛичная жалоба: <code>/report @username</code>, затем описание и необязательные фото/видео.\nЖалоба в группе: ответьте на сообщение командой <code>/report причина</code>. Бот попытается удалить команду; до удаления её могут увидеть.\n\nНа шаге доказательств /cancel открывает подтверждение. В остальных формах /cancel отменяет действие. Кнопка «В меню» отменяет незавершённую форму."""
GROUP_HELP = """🛡 <b>Команды сообщества</b>\n\n<b>Для всех:</b>\n/profile, /me — мой профиль\n/top — рейтинг чата\n/shop — магазин в личных сообщениях\n/rules — правила\n/report — ответьте на сообщение: <code>/report причина</code>\n/help — помощь\n\n<b>Для модераторов:</b>\n/warn /unwarn /mute /unmute /ban /unban /history"""

MAIN_MENU_TEXT = """👋 <b>ДОБРО ПОЖАЛОВАТЬ В NIK MODER BOT</b>
━━━━━━━━━━━━

Я помогаю владельцам и администраторам безопасно управлять группами и сообществами.

<blockquote>🛡 Антиспам и автоматическая модерация
🧩 Проверка новых участников
🚨 Жалобы с доказательствами
⭐ Активность, рейтинг и магазин
📢 Рассылки и закреплённые объявления
📣 Обязательная подписка и статистика рекламных ссылок</blockquote>

➕ Добавьте меня в группу и выдайте необходимые права администратора. Администраторы групп получают настройки своих сообществ, а центральная админ-панель доступна только владельцам бота.

<i>Выберите нужный раздел ниже.</i>"""


async def menu_links(session: AsyncSession, config: Settings) -> tuple[str | None, str | None]:
    group = await session.get(Setting, 'community_group_url')
    channel = await session.get(Setting, 'community_channel_url')
    return (
        group.value if group else getattr(config, 'community_group_url', None),
        channel.value if channel else getattr(config, 'community_channel_url', None),
    )


async def user_feature_states(session: AsyncSession) -> dict[str, bool]:
    states = await feature_states(session, ('moderation', 'daily_reward', 'reports', 'advertising', 'leaderboard', 'shop', 'points'))
    states['daily_reward'] = states['daily_reward'] and states['points']
    return states


async def private_main_markup(
    session: AsyncSession,
    bot: Bot,
    config: Settings,
    user_id: int,
) -> InlineKeyboardMarkup:
    """Build the private menu from the user's real group access."""
    me = await bot.get_me()
    group_url, channel_url = await menu_links(session, config)
    features = await user_feature_states(session)
    accesses = await chat_accesses(session, bot, user_id, config)
    return main_menu(
        config.is_owner(user_id),
        bot_username=me.username,
        group_url=group_url,
        channel_url=channel_url,
        features=features,
        has_group_settings=any(managed for _, managed in accesses),
        has_moderator_access=bool(accesses),
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, session: AsyncSession, config: Settings) -> None:
    await upsert_user(session, message.from_user)
    await message.answer(MAIN_MENU_TEXT, reply_markup=await private_main_markup(session, message.bot, config, message.from_user.id))


@router.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def group_start(message: Message) -> None:
    await message.answer("🛡 <b>Nik Moder Bot</b>\n\nЯ слежу за модерацией и активностью этого чата.\n\nИспользуйте /help, чтобы посмотреть доступные команды.")


@router.message(Command("help"), F.chat.type == "private")
async def private_help(message: Message) -> None:
    await message.answer(PRIVATE_HELP, reply_markup=back_button("nav:private_main"))


@router.message(Command("help"), F.chat.type.in_({"group", "supergroup"}))
async def group_help(message: Message) -> None:
    await message.answer(GROUP_HELP, reply_markup=group_help_menu())


@router.callback_query(F.data.in_({"menu:home", "nav:private_main"}), F.message.chat.type == "private")
async def menu_home(callback: CallbackQuery, config: Settings, bot: Bot, session: AsyncSession) -> None:
    await callback.answer()
    markup = await private_main_markup(session, bot, config, callback.from_user.id)
    if callback.message.text:
        await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=markup)
    else:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(MAIN_MENU_TEXT, reply_markup=markup)


@router.callback_query(F.data == 'menu:add_bot', F.message.chat.type == 'private')
async def add_bot(callback: CallbackQuery, bot: Bot) -> None:
    me = await bot.get_me()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='➕ Выбрать группу', url=f'https://t.me/{me.username}?startgroup=true')],
        [InlineKeyboardButton(text='⬅️ Назад', callback_data='nav:private_main')],
    ])
    await callback.answer()
    await callback.message.edit_text(
        '➕ <b>ДОБАВИТЬ БОТА В НОВУЮ ГРУППУ</b>\n\n'
        'Нажмите «Выбрать группу», если подключаете ещё одну группу. После добавления назначьте бота администратором.\n\n'
        '<b>Бот уже в вашей группе?</b> Вернитесь назад и откройте «⚙️ Настройки группы». '
        'Простого присутствия в чате недостаточно: для антиспама и жалоб нужны права удаления сообщений, '
        'для мутов и проверки новичков — ограничение участников, для объявлений — закрепление сообщений.',
        reply_markup=markup,
    )


@router.callback_query(F.data == 'menu:info', F.message.chat.type == 'private')
async def bot_info(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text('''ℹ️ <b>ИНФОРМАЦИЯ О БОТЕ</b>
━━━━━━━━━━━━

<b>Nik Moder Bot</b> — центр управления Telegram-сообществом.

<blockquote>⚡ Удаляет спам и повторяющиеся сообщения
🔨 Поддерживает предупреждения, муты и баны
🧩 Проверяет новичков перед допуском в чат
🚨 Принимает личные и групповые жалобы
📊 Считает активность и формирует рейтинг
🛍 Поддерживает магазин и выдачу товаров
📣 Проверяет обязательную подписку на каналы и чаты</blockquote>

🔐 <b>Доступы разделены:</b>
• участникам — профиль, рейтинг, магазин и жалобы;
• администраторам групп — команды модерации и проверка прав;
• владельцам бота — супер-админка, рассылки и глобальная реклама.

<i>Для полноценной защиты группы боту нужны права удаления сообщений и ограничения участников.</i>''', reply_markup=back_button('nav:private_main'))


@router.callback_query(F.data == 'menu:support', F.message.chat.type == 'private')
async def removed_support(callback: CallbackQuery, bot: Bot, session: AsyncSession, config: Settings) -> None:
    """Gracefully replace keyboards sent before the Support button was removed."""
    await callback.answer('Раздел поддержки убран')
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=await private_main_markup(session, bot, config, callback.from_user.id))


@router.callback_query(F.data.in_({'menu:group', 'menu:channel'}), F.message.chat.type == 'private')
async def community_link(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    is_group = callback.data == 'menu:group'
    group_url, channel_url = await menu_links(session, config)
    configured = group_url if is_group else channel_url
    rows = []
    if configured:
        rows.append([InlineKeyboardButton(text='Открыть', url=configured)])
    elif is_group:
        chats = (await session.scalars(select(Chat).where(Chat.is_active.is_(True), Chat.username.is_not(None)).order_by(Chat.id).limit(20))).all()
        rows.extend([[InlineKeyboardButton(text=chat.title[:60], url=f'https://t.me/{chat.username}')]] for chat in chats)
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='nav:private_main')])
    title = '👥 <b>Группа</b>' if is_group else '📣 <b>Канал</b>'
    empty = 'Группа пока не настроена. Владелец может добавить ссылку: /admin → «Ссылки меню».' if is_group else 'Канал пока не настроен. Владелец может добавить ссылку: /admin → «Ссылки меню».'
    await callback.answer()
    await callback.message.edit_text(title + ('\n\nВыберите сообщество.' if len(rows) > 1 else '\n\n' + empty), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == 'menu:group_settings', F.message.chat.type == 'private')
async def group_settings(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    chats = (await session.scalars(select(Chat).where(Chat.is_active.is_(True)).order_by(Chat.title).limit(100))).all()
    available = []
    unavailable = 0
    for chat in chats:
        if not await can_manage_chat(bot, chat, callback.from_user.id, config):
            continue
        try:
            bot_member = await bot.get_chat_member(chat.telegram_id, bot.id)
        except TelegramAPIError:
            unavailable += 1
            continue
        if bot_member.status in {'left', 'kicked'}:
            chat.is_active = False
            unavailable += 1
            continue
        available.append(chat)
    rows = [[InlineKeyboardButton(text=f'⚙️ {chat.title[:55]}', callback_data=f'menu:group_settings:{chat.id}')] for chat in available]
    rows.append([InlineKeyboardButton(text='➕ Добавить новую группу', callback_data='menu:add_bot')])
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='nav:private_main')])
    await callback.answer()
    text = '⚙️ <b>НАСТРОЙКИ ГРУПП</b>\n━━━━━━━━━━━━\n\n'
    text += 'Выберите группу, которой вы владеете.' if available else 'Доступных групп пока нет.'
    if unavailable:
        text += f'\n\n⚠️ Недоступных или удалённых групп скрыто: <b>{unavailable}</b>.'
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('menu:group_settings:'), F.message.chat.type == 'private')
async def group_settings_detail(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except ValueError:
        await callback.answer('Некорректная группа', show_alert=True); return
    chat = await session.get(Chat, chat_id)
    if not chat or not chat.is_active:
        await callback.answer('Группа недоступна', show_alert=True); return
    if not await can_manage_chat(bot, chat, callback.from_user.id, config):
        await callback.answer('Настройки доступны владельцу группы.', show_alert=True); return
    try:
        bot_member = await bot.get_chat_member(chat.telegram_id, bot.id)
    except TelegramAPIError:
        await callback.answer()
        await callback.message.edit_text(
            f'⚠️ <b>ГРУППА НЕДОСТУПНА</b>\n━━━━━━━━━━━━\n\n💬 {escape(chat.title)}\n<code>{chat.telegram_id}</code>\n\nTelegram не дал проверить права бота. Возможно, бот удалён из группы, группа была преобразована или используется старая запись.\n\nДобавьте бота заново либо выберите другую группу.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='➕ Добавить бота заново', callback_data='menu:add_bot', style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text='⬅️ К группам', callback_data='menu:group_settings')],
            ]),
        )
        return
    if bot_member.status in {'left', 'kicked'}:
        chat.is_active = False
        await session.commit()
        await callback.answer()
        await callback.message.edit_text(
            f'⚠️ <b>БОТ УДАЛЁН ИЗ ГРУППЫ</b>\n\n💬 {escape(chat.title)}\n\nДобавьте бота обратно, чтобы восстановить настройки.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='➕ Добавить бота', callback_data='menu:add_bot', style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton(text='⬅️ К группам', callback_data='menu:group_settings')],
            ]),
        )
        return
    creator = bot_member.status == 'creator'
    administrator = creator or bot_member.status == 'administrator'
    delete = creator or bool(getattr(bot_member, 'can_delete_messages', False))
    restrict = creator or bool(getattr(bot_member, 'can_restrict_members', False))
    pin = creator or bool(getattr(bot_member, 'can_pin_messages', False))
    pro = await chat_has_pro(session, chat.id, callback.from_user.id, config)
    pro_states = await protection_states(session, chat.id)
    pro_badge = '💎 <b>PRO активен навсегда</b>' if pro else f'🔒 <b>PRO не активирован</b> · {config.premium_price_stars} ⭐ навсегда'
    text = f'''⚙️ <b>ПАРАМЕТРЫ ГРУППЫ</b>
━━━━━━━━━━━━

💬 Группа: <b>{escape(chat.title)}</b>
{pro_badge}
🤖 Роль бота: <b>{"администратор" if administrator else "обычный участник"}</b>

{'' if administrator else '<blockquote>⚠️ Бот подключён к группе, но не назначен администратором. Поэтому настройки видны, а действия модерации выполнять нельзя.</blockquote>'}

<b>БАЗОВАЯ ЗАЩИТА</b>
{"✅" if delete else "❌"} Антиспам и удаление сообщений
{"✅" if restrict else "❌"} Варны, муты, баны и капча
{"✅" if pin else "❌"} Закрепление объявлений
✅ Жалобы, профиль и рейтинг

<b>PRO-ЗАЩИТА</b>
{"✅" if pro else "🔒"} Медиа-фильтр, запрещённые слова и фильтр 18+
{"✅" if pro else "🔒"} Массовый вход и блокировка ботов
{"✅" if pro else "🔒"} Скрытые отправители
{"✅" if pro else "🔒"} Все будущие PRO-модули без доплаты

<i>✅ — включено, ❌ — выключено, 🔒 — требуется PRO.</i>'''
    rows = [
        [InlineKeyboardButton(text='📊 Статистика', callback_data=f'groupcfg:stats:{chat.id}'), InlineKeyboardButton(text='🛡 Модераторы', callback_data=f'moder:chat:{chat.id}')],
        [InlineKeyboardButton(text='📜 Правила', callback_data=f'groupcfg:rules:{chat.id}'), InlineKeyboardButton(text='⚠️ Команды', callback_data='menu:help')],
        [InlineKeyboardButton(text='📢 Обязательная подписка / реклама', callback_data=f'subcfg:group:{chat.id}')],
    ]
    for offset in range(0, len(GROUP_CONTROLS), 2):
        row = []
        for item in GROUP_CONTROLS[offset:offset + 2]:
            locked = item.premium and not pro
            status = '🔒' if locked else ('✅' if pro_states[item.key] else '❌')
            row.append(InlineKeyboardButton(
                text=f'{status} {item.title.split(" ", 1)[1]}',
                callback_data=f'premium:info:{chat.id}' if locked else f'groupcfg:view:{item.key}:{chat.id}',
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(text=('💎 PRO активен навсегда' if pro else f'🔒 Купить все PRO · {config.premium_price_stars} ⭐'), callback_data=f'premium:info:{chat.id}', style=ButtonStyle.SUCCESS)])
    rows.append([InlineKeyboardButton(text='🔄 Проверить права бота', callback_data=f'menu:group_settings:{chat.id}')])
    if chat.username:
        rows.append([InlineKeyboardButton(text='👥 Открыть группу', url=f'https://t.me/{chat.username}')])
    rows.append([InlineKeyboardButton(text='⬅️ К группам', callback_data='menu:group_settings')])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "menu:help", F.message.chat.type == "private")
async def menu_help(callback: CallbackQuery, config: Settings) -> None:
    await callback.answer()
    await callback.message.edit_text(PRIVATE_HELP, reply_markup=back_button("nav:private_main"))


@router.message(Command("rules"))
@router.callback_query(F.data == "menu:rules")
@router.callback_query(F.data == "group:rules")
async def rules(event: Message | CallbackQuery, session: AsyncSession) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    result = None
    if message.chat.type in {'group', 'supergroup'}:
        chat = await session.scalar(select(Chat).where(Chat.telegram_id == message.chat.id))
        if chat:
            result = await session.scalar(select(ChatProtectionSetting.value).where(ChatProtectionSetting.chat_id == chat.id, ChatProtectionSetting.key == 'rules_text'))
    if not result:
        result = await session.scalar(select(Setting.value).where(Setting.key == "rules"))
    text = "📜 <b>Правила</b>\n\n" + escape(result or DEFAULT_RULES)
    if isinstance(event, CallbackQuery):
        await event.answer()
        is_group = event.message.chat.type in {"group", "supergroup"}
        await event.message.edit_text(text, reply_markup=back_button("nav:group_help" if is_group else "nav:private_main"))
    else:
        await event.answer(text)


@router.callback_query(F.data == "nav:group_help", F.message.chat.type.in_({"group", "supergroup"}))
async def group_help_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(GROUP_HELP, reply_markup=group_help_menu())


@router.message(Command("profile", "me"))
@router.callback_query(F.data == "menu:profile", F.message.chat.type == "private")
async def profile(event: Message | CallbackQuery, session: AsyncSession, config: Settings, bot: Bot) -> None:
    message = event.message if isinstance(event, CallbackQuery) else event
    subject = message.reply_to_message.from_user if isinstance(event, Message) and message.reply_to_message else event.from_user
    if isinstance(event, Message) and event.text and len(event.text.split()) > 1 and config.is_owner(event.from_user.id):
        raw = event.text.split(maxsplit=1)[1].lstrip("@")
        subject_db = await session.scalar(select(User).where(User.username.ilike(raw) if not raw.isdigit() else User.telegram_id == int(raw)))
    else:
        subject_db = await upsert_user(session, subject)
    features = await feature_states(session, ('points', 'leaderboard'))
    if not subject_db:
        text = "Пользователь не найден в базе. Ответьте командой на его сообщение или используйте Telegram ID."
    else:
        name = escape(' '.join(filter(None, (subject_db.first_name, subject_db.last_name))))
        username = f"@{escape(subject_db.username)}" if subject_db.username else "—"
        if isinstance(event, Message) and event.chat.type in {"group", "supergroup"}:
            chat = await session.scalar(select(Chat).where(Chat.telegram_id == event.chat.id))
            stats = await session.scalar(select(UserChatStats).where(UserChatStats.user_id == subject_db.id, UserChatStats.chat_id == chat.id)) if chat else None
            message_count = stats.message_count if stats else 0
            points = stats.points_earned if stats else 0
            active_warns = len((await session.scalars(select(Warning.id).where(Warning.chat_id == chat.id, Warning.target_user_id == subject_db.id, Warning.is_active.is_(True)))).all()) if chat else 0
            rank = (await session.scalar(select(func.count(UserChatStats.id)).where(UserChatStats.chat_id == chat.id, UserChatStats.points_earned > points)) or 0) + 1 if chat else 1
            bar, next_level = level_progress(points)
            points_line = f'⭐ Очки чата: <b>{points:,}</b>\n🏅 {level_for_points(points)}\n<code>{bar}</code>\n{next_level}' if features['points'] else '⛔ Начисление очков отключено'
            rank_line = f'🏆 Место в чате: <b>#{rank}</b>' if features['leaderboard'] else '🏆 Рейтинг отключён'
            text = f"👤 <b>ПРОФИЛЬ УЧАСТНИКА</b>\n━━━━━━━━━━━━\n\n<b>{name}</b>\n{username} · <code>{subject_db.telegram_id}</code>\n\n<b>АКТИВНОСТЬ В ЧАТЕ</b>\n💬 Сообщений: <b>{message_count:,}</b>\n{points_line}\n{rank_line}\n\n<b>РЕПУТАЦИЯ</b>\n{'🟢' if not active_warns else '🟡'} Предупреждения: <b>{active_warns}/3</b>"
        else:
            since = subject_db.created_at.strftime("%d.%m.%Y")
            active_warns = len((await session.scalars(select(Warning.id).where(Warning.target_user_id == subject_db.id, Warning.is_active.is_(True)))).all())
            rank = (await session.scalar(select(func.count(User.id)).where(User.points > subject_db.points)) or 0) + 1
            reward = await session.scalar(select(DailyReward).where(DailyReward.user_id == subject_db.id).order_by(DailyReward.day.desc()).limit(1))
            bar, next_level = level_progress(subject_db.points)
            points_block = f'⭐ Баланс: <b>{subject_db.points:,}</b>\n🏅 {level_for_points(subject_db.points)}\n<code>{bar}</code>\n{next_level}' if features['points'] else '⛔ Начисление очков отключено'
            rank_line = f'🏆 Место: <b>#{rank}</b>' if features['leaderboard'] else '🏆 Рейтинг отключён'
            text = f"👤 <b>МОЙ ПРОФИЛЬ</b>\n━━━━━━━━━━━━\n\n<b>{name}</b>\n{username} · <code>{subject_db.telegram_id}</code>\n📅 С нами с {since}\n\n<b>ПРОГРЕСС</b>\n{points_block}\n{rank_line}\n🔥 Серия наград: <b>{reward.streak if reward else 0} дн.</b>\n\n<b>АКТИВНОСТЬ</b>\n💬 Сообщений: <b>{subject_db.message_count:,}</b>\n{'🟢' if not active_warns else '🟡'} Предупреждения: <b>{active_warns}/3</b>"
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.edit_text(text, reply_markup=back_button())
    else:
        if event.chat.type in {"group", "supergroup"}:
            await bot.send_message(event.chat.id, text, ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=event.from_user.id))
        else:
            await event.answer(text, reply_markup=back_button("nav:private_main"))
