from __future__ import annotations

from datetime import date, datetime, timezone

from aiogram.types import Message
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ActivityMessage, DailyActivity, User, UserChatStats
from app.services.users import upsert_chat, upsert_user


async def record_message_activity(session: AsyncSession, message: Message, *, award_points: bool = True) -> None:
    """Atomically count a non-bot, new group message exactly once."""
    if not message.from_user or message.from_user.is_bot or message.chat.type == "private":
        return
    user = await upsert_user(session, message.from_user)
    chat = await upsert_chat(session, message.chat)
    marker = await session.execute(
        insert(ActivityMessage).values(
            chat_id=chat.id, user_id=user.id, telegram_message_id=message.message_id
        ).on_conflict_do_nothing(constraint="uq_activity_message")
    )
    if marker.rowcount != 1:
        return

    now = datetime.now(timezone.utc)
    point_delta = 1 if award_points else 0
    await session.execute(
        update(User).where(User.id == user.id).values(
            message_count=User.message_count + 1, points=User.points + point_delta, last_seen_at=now
        )
    )
    await session.execute(
        insert(UserChatStats).values(
            user_id=user.id, chat_id=chat.id, message_count=1, points_earned=point_delta, last_message_at=now
        ).on_conflict_do_update(
            constraint="uq_user_chat_stats",
            set_={
                "message_count": UserChatStats.message_count + 1,
                "points_earned": UserChatStats.points_earned + point_delta,
                "last_message_at": now,
            },
        )
    )
    await session.execute(
        insert(DailyActivity).values(user_id=user.id, chat_id=chat.id, day=date.today(), message_count=1, points_earned=point_delta)
        .on_conflict_do_update(
            constraint="uq_daily_activity",
            set_={
                "message_count": DailyActivity.message_count + 1,
                "points_earned": DailyActivity.points_earned + point_delta,
            },
        )
    )
