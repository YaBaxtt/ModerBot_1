from __future__ import annotations

from datetime import date, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Chat, DailyActivity, User, UserChatStats
from app.services.text import user_mention_html

router = Router(name="leaderboard")


def keyboard(metric: str, period: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Сообщения", callback_data=f"top:messages:{period}"), InlineKeyboardButton(text="⭐ Очки", callback_data=f"top:points:{period}")],
        [InlineKeyboardButton(text="🕐 Сегодня", callback_data=f"top:{metric}:today"), InlineKeyboardButton(text="📅 Месяц", callback_data=f"top:{metric}:month"), InlineKeyboardButton(text="🏆 Всё время", callback_data=f"top:{metric}:all")],
    ])


async def build_top(session: AsyncSession, metric: str, period: str, chat_telegram_id: int | None = None) -> str:
    column = User.message_count if metric == "messages" else User.points
    title = "сообщениям" if metric == "messages" else "очкам"
    chat = await session.scalar(select(Chat).where(Chat.telegram_id == chat_telegram_id)) if chat_telegram_id else None
    if chat_telegram_id is not None and chat is None:
        return "🏆 <b>Рейтинг чата</b>\n\nПока нет данных."
    if period == "all":
        if chat:
            stats_column = UserChatStats.message_count if metric == "messages" else UserChatStats.points_earned
            rows = (await session.execute(select(User.first_name, User.telegram_id, User.username, stats_column).join(UserChatStats, UserChatStats.user_id == User.id).where(UserChatStats.chat_id == chat.id).order_by(desc(stats_column)).limit(10))).all()
        else:
            rows = (await session.execute(select(User.first_name, User.telegram_id, User.username, column).order_by(desc(column)).limit(10))).all()
        period_name = "всё время"
    else:
        daily_column = DailyActivity.message_count if metric == "messages" else DailyActivity.points_earned
        since = date.today().replace(day=1) if period == "month" else date.today()
        conditions = [DailyActivity.day >= since]
        if chat:
            conditions.append(DailyActivity.chat_id == chat.id)
        rows = (await session.execute(select(User.first_name, User.telegram_id, User.username, func.coalesce(func.sum(daily_column), 0)).join(DailyActivity, DailyActivity.user_id == User.id).where(*conditions).group_by(User.id).order_by(desc(func.sum(daily_column))).limit(10))).all()
        period_name = "текущий месяц" if period == "month" else "сегодня"
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[index] if index < 3 else f'{index + 1}.'} {user_mention_html(first_name=name, telegram_id=telegram_id, username=username)} — {value:,}" for index, (name, telegram_id, username, value) in enumerate(rows)]
    return f"🏆 <b>Топ по {title}</b>\nПериод: {period_name}\n\n" + ("\n".join(lines) if lines else "Пока нет данных.")


@router.message(Command("top"))
async def top_command(message: Message, session: AsyncSession) -> None:
    chat_id = message.chat.id if message.chat.type in {"group", "supergroup"} else None
    await message.answer(await build_top(session, "points", "all", chat_id), reply_markup=keyboard("points", "all"))


@router.callback_query(F.data.startswith("top:"))
async def top_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    _, metric, period = callback.data.split(":")
    await callback.answer()
    chat_id = callback.message.chat.id if callback.message.chat.type in {"group", "supergroup"} else None
    await callback.message.edit_text(await build_top(session, metric, period, chat_id), reply_markup=keyboard(metric, period))


@router.callback_query(F.data == "group:top", F.message.chat.type.in_({"group", "supergroup"}))
async def group_top_callback(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await callback.message.edit_text(await build_top(session, "points", "all", callback.message.chat.id), reply_markup=keyboard("points", "all"))
