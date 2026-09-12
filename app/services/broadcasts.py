from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import Broadcast, BroadcastTarget, Chat, User
from app.keyboards.common import back_button
from app.services.features import feature_enabled

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def broadcast_markup(button_text: str | None, button_url: str | None) -> InlineKeyboardMarkup | None:
    if not button_text or not button_url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]])


async def deliver_broadcast(bot: Bot, session: AsyncSession, broadcast: Broadcast) -> tuple[int, int]:
    if broadcast.target == BroadcastTarget.USERS:
        recipients = (await session.scalars(select(User.telegram_id))).all()
    else:
        recipients = (await session.scalars(select(Chat.telegram_id).where(Chat.is_active.is_(True)))).all()
    delivered = failed = 0
    markup = broadcast_markup(broadcast.button_text, broadcast.button_url)
    for recipient in recipients:
        try:
            if broadcast.photo_file_id:
                if broadcast.text and len(broadcast.text) > 1024:
                    await bot.send_photo(recipient, broadcast.photo_file_id)
                    await bot.send_message(recipient, broadcast.text, reply_markup=markup)
                else:
                    await bot.send_photo(recipient, broadcast.photo_file_id, caption=broadcast.text, reply_markup=markup)
            else:
                await bot.send_message(recipient, broadcast.text or "", reply_markup=markup)
            delivered += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                if broadcast.photo_file_id:
                    if broadcast.text and len(broadcast.text) > 1024:
                        await bot.send_photo(recipient, broadcast.photo_file_id)
                        await bot.send_message(recipient, broadcast.text, reply_markup=markup)
                    else:
                        await bot.send_photo(recipient, broadcast.photo_file_id, caption=broadcast.text, reply_markup=markup)
                else:
                    await bot.send_message(recipient, broadcast.text or "", reply_markup=markup)
                delivered += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:
            failed += 1
            log.warning("Broadcast delivery failed for %s", recipient, exc_info=True)
        await asyncio.sleep(0.04)
    broadcast.delivered_count = delivered
    broadcast.failed_count = failed
    broadcast.sent_at = datetime.now()
    return delivered, failed


async def scheduled_delivery(bot: Bot, session_factory: async_sessionmaker[AsyncSession], broadcast_id: int) -> None:
    async with session_factory() as session:
        if not await feature_enabled(session, 'broadcasts'):
            log.info('Broadcast %s paused because broadcasts are disabled', broadcast_id)
            return
        broadcast = await session.get(Broadcast, broadcast_id)
        if not broadcast or broadcast.sent_at:
            return
        delivered, failed = await deliver_broadcast(bot, session, broadcast)
        await session.commit()
        creator = await session.get(User, broadcast.creator_user_id) if broadcast.creator_user_id else None
        if creator:
            try:
                await bot.send_message(creator.telegram_id, f"📢 Рассылка #{broadcast_id} завершена.\n✅ Доставлено: {delivered}\n⚠️ Не доставлено: {failed}", reply_markup=back_button('admin:home'))
            except TelegramAPIError:
                log.warning("Could not deliver broadcast summary %s", broadcast_id)


def schedule_delivery(bot: Bot, session_factory: async_sessionmaker[AsyncSession], broadcast_id: int, run_at: datetime) -> None:
    scheduler.add_job(scheduled_delivery, trigger="date", run_date=run_at, args=[bot, session_factory, broadcast_id], id=f"broadcast:{broadcast_id}", replace_existing=True, misfire_grace_time=None)


async def start_broadcast_scheduler(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    if not scheduler.running:
        scheduler.start()
    async with session_factory() as session:
        if not await feature_enabled(session, 'broadcasts'):
            return
        pending = (await session.scalars(select(Broadcast).where(Broadcast.sent_at.is_(None), Broadcast.scheduled_at.is_not(None)))).all()
        now = datetime.now()
        for broadcast in pending:
            schedule_delivery(bot, session_factory, broadcast.id, max(broadcast.scheduled_at.replace(tzinfo=None), now))


async def sync_broadcast_jobs(bot: Bot, session_factory: async_sessionmaker[AsyncSession], enabled: bool) -> int:
    jobs = [job for job in scheduler.get_jobs() if job.id.startswith('broadcast:')]
    for job in jobs:
        job.remove()
    if not enabled:
        return len(jobs)
    async with session_factory() as session:
        pending = (await session.scalars(select(Broadcast).where(Broadcast.sent_at.is_(None), Broadcast.scheduled_at.is_not(None)))).all()
        now = datetime.now()
        for broadcast in pending:
            schedule_delivery(bot, session_factory, broadcast.id, max(broadcast.scheduled_at.replace(tzinfo=None), now))
        return len(pending)
