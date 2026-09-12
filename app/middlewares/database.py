from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DatabaseMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message:
                    await session.commit()
                    return
                await session.rollback()
                raise
            except Exception:
                await session.rollback()
                logging.getLogger(__name__).exception("Update handler failed; transaction rolled back")
                raise
