from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(
    is_owner: bool = False,
    *,
    bot_username: str | None = None,
    group_url: str | None = None,
    channel_url: str | None = None,
    features: dict[str, bool] | None = None,
    has_group_settings: bool = True,
    has_moderator_access: bool = True,
) -> InlineKeyboardMarkup:
    def label(text: str, key: str) -> str:
        return f'⛔ {text}' if features is not None and not features.get(key, True) else text
    rows = [
        [InlineKeyboardButton(text="➕ Добавить меня в группу ➕", url=f"https://t.me/{bot_username}?startgroup=true", style=ButtonStyle.SUCCESS) if bot_username else InlineKeyboardButton(text="➕ Добавить меня в группу ➕", callback_data="menu:add_bot", style=ButtonStyle.SUCCESS)],
    ]
    if has_group_settings:
        rows.append([InlineKeyboardButton(text="⚙️ Настройки группы", callback_data="menu:group_settings", style=ButtonStyle.SUCCESS)])
    rows.extend([
        [InlineKeyboardButton(text="👥 Группа", url=group_url) if group_url else InlineKeyboardButton(text="👥 Группа", callback_data="menu:group"), InlineKeyboardButton(text="📣 Канал", url=channel_url) if channel_url else InlineKeyboardButton(text="📣 Канал", callback_data="menu:channel")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"), InlineKeyboardButton(text="ℹ️ Информация", callback_data="menu:info")],
        [InlineKeyboardButton(text=label("🎁 Бонус", 'daily_reward'), callback_data="daily:claim"), InlineKeyboardButton(text=label("🏆 Топ", 'leaderboard'), callback_data="top:points:all")],
        [InlineKeyboardButton(text=label("🛍 Магазин", 'shop'), callback_data="shop:list"), InlineKeyboardButton(text=label("💼 Реклама", 'advertising'), callback_data="ads:start")],
        [InlineKeyboardButton(text="📜 Правила", callback_data="menu:rules"), InlineKeyboardButton(text="ℹ️ Помощь", callback_data="menu:help")],
        [InlineKeyboardButton(text=label("🚨 Пожаловаться", 'reports'), callback_data="menu:report_help")],
    ])
    if has_moderator_access:
        rows.append([InlineKeyboardButton(text=label("🛡 Модераторская", 'moderation'), callback_data="moder:home")])
    if is_owner:
        rows.append([InlineKeyboardButton(text="👑 Супер-админка", callback_data="admin:home", style=ButtonStyle.SUCCESS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 Центр внимания", callback_data="admin:attention", style=ButtonStyle.SUCCESS), InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💬 Чаты", callback_data="admin:chats"), InlineKeyboardButton(text="🧩 Функции", callback_data="admin:features", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🚨 Жалобы", callback_data="admin:reports"), InlineKeyboardButton(text="🩺 Проверить бота", callback_data="admin:health", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast:start"), InlineKeyboardButton(text="📌 Объявление", callback_data="announce:start")],
        [InlineKeyboardButton(text="🛍 Добавить товар", callback_data="admin:shop_add"), InlineKeyboardButton(text="⚙️ Правила", callback_data="admin:rules")],
        [InlineKeyboardButton(text="🔗 Ссылки меню", callback_data="admin:links"), InlineKeyboardButton(text="📋 Команды", callback_data="admin:commands")],
        [InlineKeyboardButton(text="📢 Обязательная подписка", callback_data="subcfg:global")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:private_main")],
    ])


def back_button(callback_data: str = "menu:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]])


def group_help_menu() -> InlineKeyboardMarkup:
    """This keyboard deliberately has no private-menu callbacks."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Правила", callback_data="group:rules"), InlineKeyboardButton(text="🏆 Топ", callback_data="group:top")],
    ])
