import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, DailyReward, User, UserChatStats
from app.middlewares.command_cleanup import CommandCleanupMiddleware
from app.middlewares.feature_gate import FeatureGateMiddleware
from app.services.activity import record_message_activity
from app.services.features import FEATURES, feature_states, set_feature
from app.services.profile import level_progress
from app.services.rewards import claim_daily_reward
from app.handlers.admin import attention_center, render_feature_panel, stats, toggle_feature


class FeaturesAndRewardsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'test.db').as_posix())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_feature_switches_default_on_and_persist(self):
        async with self.factory() as session:
            self.assertTrue(all((await feature_states(session)).values()))
            await set_feature(session, 'shop', False)
            await session.commit()
        async with self.factory() as session:
            states = await feature_states(session)
            self.assertFalse(states['shop'])
            self.assertEqual(len(states), len(FEATURES))

    async def test_disabled_feature_never_reaches_handler(self):
        async with self.factory() as session:
            await set_feature(session, 'shop', False)
            await session.commit()
            callback = SimpleNamespace(data='shop:list', answer=AsyncMock(), message=SimpleNamespace(chat=SimpleNamespace(type='private')))
            handler = AsyncMock()
            await FeatureGateMiddleware()(handler, SimpleNamespace(callback_query=callback, message=None), {'session': session})
            handler.assert_not_awaited()
            callback.answer.assert_awaited_once()

    async def test_command_cleanup_can_be_disabled(self):
        async with self.factory() as session:
            await set_feature(session, 'command_cleanup', False)
            await session.commit()
            message = SimpleNamespace(chat=SimpleNamespace(type='supergroup'), text='/help', caption=None, delete=AsyncMock())
            handler = AsyncMock()
            await CommandCleanupMiddleware()(handler, SimpleNamespace(message=message), {'session': session})
            message.delete.assert_not_awaited()
            handler.assert_awaited_once()

    async def test_messages_count_without_awarding_points(self):
        telegram_user = TelegramUser(id=123, is_bot=False, first_name='User')
        telegram_chat = TelegramChat(id=-100, type='supergroup', title='Chat')
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=telegram_chat, from_user=telegram_user, text='hello')
        async with self.factory() as session:
            await record_message_activity(session, message, award_points=False)
            await session.commit()
        async with self.factory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 123))
            stats = await session.scalar(select(UserChatStats).where(UserChatStats.user_id == user.id))
            self.assertEqual((user.message_count, user.points), (1, 0))
            self.assertEqual((stats.message_count, stats.points_earned), (1, 0))

    async def test_daily_reward_is_once_per_day_and_keeps_streak(self):
        async with self.factory() as session:
            user = User(telegram_id=123, first_name='User', points=0)
            session.add(user)
            await session.flush()
            first, claimed = await claim_daily_reward(session, user, date(2026, 9, 9))
            self.assertTrue(claimed)
            duplicate, claimed_again = await claim_daily_reward(session, user, date(2026, 9, 9))
            self.assertFalse(claimed_again)
            self.assertEqual(duplicate.id, first.id)
            second, claimed_second = await claim_daily_reward(session, user, date(2026, 9, 10))
            self.assertTrue(claimed_second)
            self.assertEqual(second.streak, 2)
            await session.commit()
            await session.refresh(user)
            self.assertEqual(user.points, first.reward + second.reward)
            self.assertEqual(len((await session.scalars(select(DailyReward))).all()), 2)

    async def test_profile_progress_bar_is_bounded(self):
        self.assertEqual(len(level_progress(0)[0]), 10)
        self.assertEqual(len(level_progress(499)[0]), 10)
        self.assertIn('Максимальный', level_progress(5000)[1])

    async def test_new_admin_dashboards_render_on_empty_database(self):
        config = SimpleNamespace(is_owner=lambda uid: uid == 999)
        callback = SimpleNamespace(from_user=SimpleNamespace(id=999), answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        async with self.factory() as session:
            await stats(callback, session, config)
            self.assertIn('ПАНЕЛЬ СТАТИСТИКИ', callback.message.edit_text.call_args.args[0])
            await attention_center(callback, session, config)
            self.assertIn('ЦЕНТР ВНИМАНИЯ', callback.message.edit_text.call_args.args[0])
            await render_feature_panel(callback.message, session)
            markup = callback.message.edit_text.call_args.kwargs['reply_markup']
            self.assertEqual(len(markup.inline_keyboard), len(FEATURES) + 1)

    async def test_owner_toggle_changes_real_gate_state(self):
        callback = SimpleNamespace(data='admin:feature:shop', from_user=SimpleNamespace(id=999), answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        config = SimpleNamespace(is_owner=lambda uid: uid == 999)
        async with self.factory() as session:
            await toggle_feature(callback, session, AsyncMock(), config, self.factory)
            self.assertFalse((await feature_states(session, ('shop',)))['shop'])
            self.assertIn('❌ Отключено', callback.message.edit_text.call_args.args[0])
