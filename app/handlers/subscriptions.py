from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Chat, RequiredSubscription
from app.handlers.group_controls import managed_chat
from app.middlewares.required_subscription import subscription_keyboard, subscription_text
from app.services.parse import normalise_telegram_url
from app.services.subscriptions import missing_subscriptions, record_check, set_subscription_enabled, subscription_enabled, subscription_links, subscription_stats
from app.services.users import upsert_user


router = Router(name='subscriptions')
router.message.filter(F.chat.type == 'private')


class SubscriptionForm(StatesGroup):
    link = State()
    description = State()


def _scope_callback(owner_chat_id: int | None) -> str:
    return 'subcfg:global' if owner_chat_id is None else f'subcfg:group:{owner_chat_id}'


async def _can_configure(session: AsyncSession, bot: Bot, config: Settings, owner_chat_id: int | None, user_id: int) -> Chat | bool:
    if owner_chat_id is None:
        return config.is_owner(user_id)
    return await managed_chat(session, bot, config, owner_chat_id, user_id) or False


async def render_subscription_panel(message: Message, session: AsyncSession, owner_chat_id: int | None) -> None:
    enabled = await subscription_enabled(session, owner_chat_id)
    links = await subscription_links(session, owner_chat_id)
    scope_name = 'личного меню бота' if owner_chat_id is None else 'этой группы'
    rows = [[
        InlineKeyboardButton(text='✅ Включить', callback_data=f'subcfg:toggle:{owner_chat_id or 0}:1', style=ButtonStyle.SUCCESS if enabled else None),
        InlineKeyboardButton(text='❌ Выключить', callback_data=f'subcfg:toggle:{owner_chat_id or 0}:0', style=ButtonStyle.SUCCESS if not enabled else None),
    ]]
    rows.append([InlineKeyboardButton(text='➕ Добавить канал или чат', callback_data=f'subcfg:add:{owner_chat_id or 0}')])
    rows.extend([[InlineKeyboardButton(text=f'📢 {link.title[:52]}', callback_data=f'subcfg:item:{link.id}')]] for link in links)
    rows.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='admin:home' if owner_chat_id is None else f'menu:group_settings:{owner_chat_id}')])
    text = (
        '📢 <b>ОБЯЗАТЕЛЬНАЯ ПОДПИСКА</b>\n━━━━━━━━━━━━\n\n'
        f'Область: <b>{scope_name}</b>\n'
        f'Статус: {"✅ включено" if enabled else "❌ выключено"}\n'
        f'Активных ссылок: <b>{len(links)}</b>\n\n'
        'Пользователь увидит описание и кнопки каналов, а доступ получит после проверки подписки. '
        'Для проверки бот должен быть администратором каждого добавленного канала или чата.'
    )
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == 'subcfg:global')
async def global_panel(callback: CallbackQuery, session: AsyncSession, config: Settings) -> None:
    if not config.is_owner(callback.from_user.id):
        await callback.answer('Доступно только владельцу бота.', show_alert=True)
        return
    await callback.answer()
    await render_subscription_panel(callback.message, session, None)


@router.callback_query(F.data.startswith('subcfg:group:'))
async def group_panel(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        chat_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        await callback.answer('Некорректная группа.', show_alert=True)
        return
    if not await _can_configure(session, bot, config, chat_id, callback.from_user.id):
        await callback.answer('Нет доступа к настройкам группы.', show_alert=True)
        return
    await callback.answer()
    await render_subscription_panel(callback.message, session, chat_id)


@router.callback_query(F.data.startswith('subcfg:toggle:'))
async def toggle(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        _, _, raw_chat_id, raw_enabled = callback.data.split(':')
        owner_chat_id = int(raw_chat_id) or None
        enabled = raw_enabled == '1'
    except (AttributeError, ValueError):
        await callback.answer('Некорректная настройка.', show_alert=True)
        return
    if not await _can_configure(session, bot, config, owner_chat_id, callback.from_user.id):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    links = await subscription_links(session, owner_chat_id)
    if enabled and not links:
        await callback.answer('Сначала добавьте хотя бы один канал или чат.', show_alert=True)
        return
    await set_subscription_enabled(session, owner_chat_id, enabled)
    await session.commit()
    await callback.answer('Включено' if enabled else 'Выключено')
    await render_subscription_panel(callback.message, session, owner_chat_id)


@router.callback_query(F.data.startswith('subcfg:add:'))
async def add_start(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    try:
        owner_chat_id = int(callback.data.rsplit(':', 1)[1]) or None
    except (AttributeError, ValueError):
        await callback.answer('Некорректная настройка.', show_alert=True)
        return
    if not await _can_configure(session, bot, config, owner_chat_id, callback.from_user.id):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    await state.set_state(SubscriptionForm.link)
    await state.update_data(subscription_owner_chat_id=owner_chat_id or 0)
    await callback.answer()
    await callback.message.edit_text(
        '🔗 <b>ДОБАВЛЕНИЕ ССЫЛКИ</b>\n\nОтправьте публичную ссылку на Telegram-канал или чат.\n'
        'Пример: <code>https://t.me/example</code> или <code>@example</code>.\n\n'
        'Бот заранее должен быть добавлен туда администратором.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ Отмена', callback_data=_scope_callback(owner_chat_id))]]),
    )


@router.message(SubscriptionForm.link, F.text)
async def add_link(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data()
    owner_chat_id = int(data.get('subscription_owner_chat_id', 0)) or None
    if not await _can_configure(session, bot, config, owner_chat_id, message.from_user.id):
        await state.clear()
        await message.answer('Доступ к настройкам потерян.')
        return
    url = normalise_telegram_url(message.text)
    if not url:
        await message.answer('Нужна публичная Telegram-ссылка вида <code>https://t.me/example</code>.')
        return
    slug = urlparse(url).path.strip('/').split('/')[0]
    if not slug or slug.startswith('+') or slug in {'joinchat', 'c'}:
        await message.answer('Приватные пригласительные ссылки проверить нельзя. Используйте публичный @username.')
        return
    try:
        target = await bot.get_chat('@' + slug)
        bot_member = await bot.get_chat_member(target.id, bot.id)
    except TelegramAPIError:
        await message.answer('Не удалось открыть этот чат. Проверьте ссылку и добавьте бота туда администратором.')
        return
    if target.type not in {'channel', 'group', 'supergroup'} or bot_member.status not in {'administrator', 'creator'}:
        await message.answer('Для проверки подписки бот должен быть администратором этого канала или чата.')
        return
    existing = await session.scalar(select(RequiredSubscription).where(
        RequiredSubscription.scope == ('private' if owner_chat_id is None else 'group'),
        RequiredSubscription.owner_chat_id.is_(None) if owner_chat_id is None else RequiredSubscription.owner_chat_id == owner_chat_id,
        RequiredSubscription.target_telegram_id == target.id,
    ))
    if existing and existing.is_active:
        await message.answer('Эта ссылка уже добавлена.')
        return
    await state.update_data(
        subscription_target_id=target.id,
        subscription_title=getattr(target, 'title', None) or '@' + slug,
        subscription_url=url,
        subscription_reactivate_id=existing.id if existing else 0,
    )
    await state.set_state(SubscriptionForm.description)
    await message.answer('Теперь отправьте короткое описание рекламы до 300 символов. Для описания по умолчанию отправьте <code>-</code>.')


@router.message(SubscriptionForm.description, F.text)
async def add_description(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Settings) -> None:
    data = await state.get_data()
    owner_chat_id = int(data.get('subscription_owner_chat_id', 0)) or None
    if not await _can_configure(session, bot, config, owner_chat_id, message.from_user.id):
        await state.clear()
        await message.answer('Доступ к настройкам потерян.')
        return
    description = message.text.strip()
    if len(description) > 300:
        await message.answer('Сократите описание до 300 символов.')
        return
    creator = await upsert_user(session, message.from_user)
    existing_id = int(data.get('subscription_reactivate_id', 0))
    link = await session.get(RequiredSubscription, existing_id) if existing_id else None
    if link:
        link.title = str(data['subscription_title'])[:255]
        link.description = None if description == '-' else description
        link.url = str(data['subscription_url'])
        link.is_active = True
        link.created_by_user_id = creator.id
    else:
        session.add(RequiredSubscription(
            scope='private' if owner_chat_id is None else 'group',
            owner_chat_id=owner_chat_id,
            target_telegram_id=int(data['subscription_target_id']),
            title=str(data['subscription_title'])[:255],
            description=None if description == '-' else description,
            url=str(data['subscription_url']),
            created_by_user_id=creator.id,
        ))
    await session.commit()
    await state.clear()
    await message.answer('✅ Ссылка добавлена. Теперь её можно включить в обязательную подписку.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='📢 Открыть настройки', callback_data=_scope_callback(owner_chat_id), style=ButtonStyle.SUCCESS),
    ]]))


async def _get_authorized_link(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> RequiredSubscription | None:
    try:
        link_id = int(callback.data.rsplit(':', 1)[1])
    except (AttributeError, ValueError):
        return None
    link = await session.get(RequiredSubscription, link_id)
    if not link or not link.is_active:
        return None
    allowed = await _can_configure(session, bot, config, link.owner_chat_id, callback.from_user.id)
    return link if allowed else None


@router.callback_query(F.data.startswith('subcfg:item:'))
async def item_stats(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    link = await _get_authorized_link(callback, session, bot, config)
    if not link:
        await callback.answer('Ссылка не найдена или нет доступа.', show_alert=True)
        return
    stats = await subscription_stats(session, link.id)
    conversion = round(stats['passed'] / stats['users'] * 100, 1) if stats['users'] else 0
    text = (
        '📊 <b>СТАТИСТИКА ССЫЛКИ</b>\n━━━━━━━━━━━━\n\n'
        f'📢 <b>{escape(link.title)}</b>\n'
        f'{escape(link.description or "Без дополнительного описания")}\n\n'
        f'👁 Показов требования: <b>{stats["impressions"]}</b>\n'
        f'👤 Уникальных пользователей: <b>{stats["users"]}</b>\n'
        f'🔄 Проверок подписки: <b>{stats["checks"]}</b>\n'
        f'✅ Подписка подтверждена: <b>{stats["passed"]}</b>\n'
        f'📈 Конверсия: <b>{conversion}%</b>\n\n'
        '<i>Telegram не передаёт боту клики по обычной ссылке, поэтому учитываются показы и подтверждённые проверки.</i>'
    )
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔗 Открыть', url=link.url)],
        [InlineKeyboardButton(text='🗑 Убрать ссылку', callback_data=f'subcfg:delete:{link.id}')],
        [InlineKeyboardButton(text='⬅️ Назад', callback_data=_scope_callback(link.owner_chat_id))],
    ]))


@router.callback_query(F.data.startswith('subcfg:delete:'))
async def delete_link(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    link = await _get_authorized_link(callback, session, bot, config)
    if not link:
        await callback.answer('Ссылка не найдена или нет доступа.', show_alert=True)
        return
    owner_chat_id = link.owner_chat_id
    link.is_active = False
    await session.commit()
    if not await subscription_links(session, owner_chat_id):
        await set_subscription_enabled(session, owner_chat_id, False)
        await session.commit()
    await callback.answer('Ссылка убрана')
    await render_subscription_panel(callback.message, session, owner_chat_id)


@router.callback_query(F.data.startswith('subcheck:'))
async def check_subscription(callback: CallbackQuery, session: AsyncSession, bot: Bot, config: Settings) -> None:
    parts = (callback.data or '').split(':')
    owner_chat_id = None
    if len(parts) >= 2 and parts[1] == 'group':
        try:
            owner_chat_id, expected_user_id = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            await callback.answer('Проверка устарела.', show_alert=True)
            return
        if expected_user_id != callback.from_user.id:
            await callback.answer('Эта кнопка предназначена другому пользователю.', show_alert=True)
            return
    enabled = await subscription_enabled(session, owner_chat_id)
    links = await subscription_links(session, owner_chat_id) if enabled else []
    missing = await missing_subscriptions(bot, links, callback.from_user.id) if enabled else []
    if enabled:
        user = await upsert_user(session, callback.from_user)
        await record_check(session, links, missing, user)
        await session.commit()
    if missing:
        await callback.answer(f'Подпишитесь ещё на {len(missing)} канал(а/ов).', show_alert=True)
        if owner_chat_id is None:
            await callback.message.edit_text(subscription_text(missing, group=False), reply_markup=subscription_keyboard(missing, 'subcheck:private'))
        return
    await callback.answer('Подписка подтверждена!')
    if owner_chat_id is not None:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        return
    from app.handlers.common import MAIN_MENU_TEXT, menu_links, user_feature_states
    from app.keyboards.common import main_menu
    me = await bot.get_me()
    group_url, channel_url = await menu_links(session, config)
    features = await user_feature_states(session)
    await callback.message.edit_text(
        MAIN_MENU_TEXT,
        reply_markup=main_menu(config.is_owner(callback.from_user.id), bot_username=me.username, group_url=group_url, channel_url=channel_url, features=features),
    )


@router.message(SubscriptionForm.link)
@router.message(SubscriptionForm.description)
async def text_required(message: Message) -> None:
    await message.answer('На этом шаге нужно отправить текст.')
