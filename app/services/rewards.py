from datetime import date, timedelta
import secrets

from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DailyReward, User


async def claim_daily_reward(session: AsyncSession, user: User, today: date | None = None) -> tuple[DailyReward, bool]:
    today = today or date.today()
    existing = await session.scalar(select(DailyReward).where(DailyReward.user_id == user.id, DailyReward.day == today))
    if existing:
        return existing, False
    previous = await session.scalar(select(DailyReward).where(DailyReward.user_id == user.id).order_by(desc(DailyReward.day)).limit(1))
    streak = previous.streak + 1 if previous and previous.day == today - timedelta(days=1) else 1
    reward = 20 + secrets.randbelow(31) + min(streak, 7) * 5
    row = DailyReward(user_id=user.id, day=today, reward=reward, streak=streak)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(select(DailyReward).where(DailyReward.user_id == user.id, DailyReward.day == today))
        if existing:
            return existing, False
        raise
    await session.execute(update(User).where(User.id == user.id).values(points=User.points + reward))
    return row, True
