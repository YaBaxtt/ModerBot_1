import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Chat, User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, MemberProfileSnapshot
from app.services.member_tracking import track_member_profile


def avatar(file_unique_id):
    photos = [] if file_unique_id is None else [[SimpleNamespace(file_unique_id=file_unique_id)]]
    return SimpleNamespace(photos=photos)


class MemberTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'test.db').as_posix())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.chat = Chat(id=-100, type='supergroup', title='Test')

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_first_observation_is_silent_then_name_and_username_are_reported_plainly(self):
        bot = AsyncMock()
        bot.get_user_profile_photos.return_value = avatar('avatar-a')
        before = TelegramUser(id=111, is_bot=False, first_name='Старое', username='old_user')
        after = TelegramUser(id=111, is_bot=False, first_name='Новое', username='new_user')
        async with self.factory() as session:
            self.assertIsNone(await track_member_profile(bot, session, before, self.chat))
            await session.flush()
            text = await track_member_profile(bot, session, after, self.chat)
            await session.commit()
        self.assertIn('Старое', text)
        self.assertIn('<b>Новое</b>', text)
        self.assertIn('<code>@old_user</code> → <code>@new_user</code>', text)
        self.assertIn('tg://user?id=111', text)
        self.assertNotIn('https://t.me/', text)

    async def test_avatar_change_is_rate_limited_and_announced_after_interval(self):
        bot = AsyncMock()
        bot.get_user_profile_photos.return_value = avatar('avatar-a')
        user = TelegramUser(id=222, is_bot=False, first_name='User')
        async with self.factory() as session:
            await track_member_profile(bot, session, user, self.chat)
            await session.flush()
            # Activity inside the interval does not call Telegram again.
            self.assertIsNone(await track_member_profile(bot, session, user, self.chat))
            self.assertEqual(bot.get_user_profile_photos.await_count, 1)
            snapshot = await session.scalar(select(MemberProfileSnapshot))
            snapshot.avatar_checked_at = datetime.now(timezone.utc) - timedelta(minutes=11)
            bot.get_user_profile_photos.return_value = avatar('avatar-b')
            text = await track_member_profile(bot, session, user, self.chat)
        self.assertIn('Аватарка обновлена', text)
        self.assertEqual(bot.get_user_profile_photos.await_count, 2)

    async def test_snapshots_are_independent_per_chat(self):
        bot = AsyncMock()
        bot.get_user_profile_photos.return_value = avatar(None)
        user = TelegramUser(id=333, is_bot=False, first_name='One')
        second_chat = Chat(id=-200, type='supergroup', title='Second')
        async with self.factory() as session:
            await track_member_profile(bot, session, user, self.chat)
            await session.flush()
            changed = TelegramUser(id=333, is_bot=False, first_name='Two')
            self.assertIsNotNone(await track_member_profile(bot, session, changed, self.chat))
            self.assertIsNone(await track_member_profile(bot, session, changed, second_chat))
            await session.flush()
            rows = (await session.scalars(select(MemberProfileSnapshot))).all()
        self.assertEqual(len(rows), 2)

