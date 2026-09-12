"""Read-only checks of the bot's effective permissions in each saved chat."""
from html import escape

from aiogram.exceptions import TelegramAPIError


async def inspect_chat(bot, chat) -> str:
    title = escape(chat.title)
    try:
        member = await bot.get_chat_member(chat.telegram_id, bot.id)
        info = await bot.get_chat(chat.telegram_id)
    except TelegramAPIError:
        return f"⚠️ <b>{title}</b>\nНет доступа к чату. Проверьте, добавлен ли туда бот."
    status = member.status
    if status not in {"administrator", "creator"}:
        return f"🔴 <b>{title}</b>\nНазначьте бота администратором: сейчас он не может модерировать чат."
    restrict = status == "creator" or bool(getattr(member, "can_restrict_members", False))
    delete = status == "creator" or bool(getattr(member, "can_delete_messages", False))
    pin = status == "creator" or bool(getattr(member, "can_pin_messages", False))
    supergroup = info.type == "supergroup"
    ready = restrict and delete and pin and supergroup
    lines = [f"{'🟢' if ready else '🟡'} <b>{title}</b>"]
    lines.append(f"{'✅' if delete else '❌'} Удаление спама")
    lines.append(f"{'✅' if restrict else '❌'} Бан и удаление участников")
    lines.append(f"{'✅' if restrict and supergroup else '❌'} Мут и проверка новичков")
    lines.append(f"{'✅' if pin else '❌'} Закрепление объявлений")
    if not pin:
        lines.append('Для объявлений включите право «Закреплять сообщения».')
    if not restrict:
        lines.append("Включите право «Блокировать пользователей» / «Ограничивать участников».")
    if not delete:
        lines.append("Включите право «Удалять сообщения».")
    if not supergroup:
        lines.append("Для персонального мута нужна супергруппа.")
    return "\n".join(lines)
