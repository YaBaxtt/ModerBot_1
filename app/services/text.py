from __future__ import annotations

from html import escape

from aiogram.types import User as TelegramUser


def user_label(user: TelegramUser | object) -> str:
    first_name = str(getattr(user, "first_name", "Пользователь"))
    last_name = getattr(user, "last_name", None)
    full_name = escape(' '.join(filter(None, (first_name, str(last_name) if last_name else None))))
    username = getattr(user, "username", None)
    # ORM User.id is internal; TelegramUser.id is the public Telegram ID.
    telegram_id = getattr(user, "telegram_id", None) or getattr(user, "id", None)
    name_link = f'<a href="tg://user?id={telegram_id}">{full_name}</a>' if telegram_id else full_name
    username_text = f' · <a href="https://t.me/{escape(str(username), quote=True)}">@{escape(str(username))}</a>' if username else ''
    id_text = f' · <code>{telegram_id}</code>' if telegram_id else ''
    return f'{name_link}{username_text}{id_text}'


def user_profile_url(user: TelegramUser | object) -> str | None:
    """Best URL for a button that opens a Telegram user profile."""
    username = getattr(user, 'username', None)
    if username:
        return f'https://t.me/{username}'
    telegram_id = getattr(user, 'telegram_id', None) or getattr(user, 'id', None)
    return f'tg://user?id={telegram_id}' if telegram_id else None


def user_mention_html(*, telegram_id: int, first_name: str, username: str | None = None) -> str:
    """One safe, clickable representation used in every owner-facing screen."""
    name = escape(first_name)
    if username:
        return f'<a href="https://t.me/{escape(username, quote=True)}">{name}</a>'
    return f'<a href="tg://user?id={telegram_id}">{name}</a>'


def display_name(user: object) -> str:
    return escape(str(getattr(user, "first_name", "Пользователь")))
