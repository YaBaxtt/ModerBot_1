from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ChatPremiumAccess


PAYLOAD_PREFIX = 'group_pro'


def build_payload(chat_id: int, buyer_telegram_id: int, price_stars: int) -> str:
    return f'{PAYLOAD_PREFIX}:{chat_id}:{buyer_telegram_id}:{price_stars}'


def parse_payload(payload: str) -> tuple[int, int, int] | None:
    try:
        prefix, raw_chat_id, raw_buyer_id, raw_price = payload.split(':')
        values = int(raw_chat_id), int(raw_buyer_id), int(raw_price)
    except (AttributeError, TypeError, ValueError):
        return None
    if prefix != PAYLOAD_PREFIX or any(value <= 0 for value in values):
        return None
    return values


async def chat_has_pro(session: AsyncSession, chat_id: int, user_telegram_id: int, config: Settings) -> bool:
    if config.is_owner(user_telegram_id):
        return True
    return bool(await session.scalar(select(ChatPremiumAccess.id).where(ChatPremiumAccess.chat_id == chat_id)))
