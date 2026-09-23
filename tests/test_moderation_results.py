import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatModerator, ModerationAction, ModerationActionType, User, Warning
from app.handlers.activity import activity
from app.handlers.moderation import history, warn
from app.services.antispam import SpamDecision


def telegram_user(user_id: int, first_name: str, username: str):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=None,
        username=username,
        is_bot=False,
    )


def telegram_chat(chat_id: int = -1001):
    return SimpleNamespace(
        id=chat_id,
        type='supergroup',
        title='Test group',
        full_name=None,
        username='test_group',
    )


class ModerationResultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        database_path = (Path(self.tmp.name) / 'test.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{database_path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_third_active_warning_really_bans_and_identifies_user(self):
        moderator = telegram_user(10, 'Owner', 'owner_name')
        target = telegram_user(20, 'Spammer', 'spam_name')
        message = SimpleNamespace(
            from_user=moderator,
            reply_to_message=SimpleNamespace(from_user=target),
            chat=telegram_chat(),
            text='/warn repeated spam',
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),
            ban_chat_member=AsyncMock(),
        )
        config = SimpleNamespace(is_owner=lambda user_id: user_id == moderator.id)

        async with self.factory() as session:
            for _ in range(3):
                await warn(message, bot, session, config)
                await session.commit()

            warning_count = await session.scalar(select(func.count(Warning.id)).where(Warning.is_active.is_(True)))
            ban_count = await session.scalar(select(func.count(ModerationAction.id)).where(ModerationAction.action == ModerationActionType.BAN))

        bot.ban_chat_member.assert_awaited_once_with(message.chat.id, target.id)
        self.assertEqual(warning_count, 3)
        self.assertEqual(ban_count, 1)
        result = message.answer.call_args.args[0]
        self.assertIn('АВТОБАН: 3 ПРЕДУПРЕЖДЕНИЯ', result)
        self.assertIn(f'tg://user?id={target.id}', result)
        self.assertIn('@spam_name', result)
        self.assertIn(f'<code>{target.id}</code>', result)
        self.assertIn('3/3', result)
        self.assertIn('repeated spam', result)

    async def test_antispam_result_identifies_user_and_reason(self):
        target = telegram_user(30, 'Flooder', 'flood_name')
        message = SimpleNamespace(
            from_user=target,
            chat=telegram_chat(-2001),
            text='same',
            caption=None,
            message_id=55,
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),
            delete_message=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        config = SimpleNamespace(
            is_owner=lambda _: False,
            antispam_min_interval_seconds=1,
            antispam_repeat_limit=5,
            antispam_repeat_window_seconds=120,
            antispam_mute_duration='1h',
        )

        async with self.factory() as session:
            with patch('app.handlers.activity.guard.inspect', return_value=SpamDecision(delete=True, mute=True)):
                await activity(message, session, bot, config)
            await session.commit()

        bot.delete_message.assert_awaited_once_with(message.chat.id, message.message_id)
        bot.restrict_chat_member.assert_awaited_once()
        result = message.answer.call_args.args[0]
        self.assertIn('АВТОМУТ ЗА СПАМ', result)
        self.assertIn(f'tg://user?id={target.id}', result)
        self.assertIn('@flood_name', result)
        self.assertIn(f'<code>{target.id}</code>', result)
        self.assertIn('5 одинаковых сообщений', result)

    async def test_history_without_reply_shows_requesting_moderators_actions_only(self):
        moderator = telegram_user(10, 'Moderator', 'moderator_name')
        message = SimpleNamespace(
            from_user=moderator,
            reply_to_message=None,
            chat=telegram_chat(),
            text='/history',
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')))
        config = SimpleNamespace(is_owner=lambda _: False)

        async with self.factory() as session:
            chat = Chat(telegram_id=message.chat.id, title=message.chat.title, username=message.chat.username)
            db_moderator = User(telegram_id=moderator.id, first_name=moderator.first_name, username=moderator.username)
            target = User(telegram_id=20, first_name='Spammer', username='spam_name')
            session.add_all([chat, db_moderator, target])
            await session.flush()
            session.add_all([
                ChatModerator(chat_id=chat.id, user_id=db_moderator.id, is_active=True),
                ModerationAction(chat_id=chat.id, target_user_id=target.id, moderator_user_id=db_moderator.id, action=ModerationActionType.WARN, reason='оскорбления'),
                ModerationAction(chat_id=chat.id, target_user_id=target.id, moderator_user_id=None, action=ModerationActionType.MUTE, reason='Автоматически: спам'),
            ])
            await session.commit()
            await history(message, bot, session, config)

        result = message.answer.call_args.args[0]
        self.assertIn('ИСТОРИЯ МОДЕРАТОРА', result)
        self.assertIn('@moderator_name', result)
        self.assertIn('Варн', result)
        self.assertNotIn('Мут', result)
        self.assertNotIn('автоматика бота', result)
        self.assertIn('@spam_name', result)

    async def test_history_reply_selects_that_moderators_actions(self):
        requester = telegram_user(10, 'Requester', 'requester_name')
        selected = telegram_user(11, 'Selected moderator', 'selected_mod')
        message = SimpleNamespace(
            from_user=requester,
            reply_to_message=SimpleNamespace(from_user=selected),
            chat=telegram_chat(),
            text='/history',
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')))
        config = SimpleNamespace(is_owner=lambda _: False)

        async with self.factory() as session:
            chat = Chat(telegram_id=message.chat.id, title=message.chat.title, username=message.chat.username)
            db_requester = User(telegram_id=requester.id, first_name=requester.first_name, username=requester.username)
            db_selected = User(telegram_id=selected.id, first_name=selected.first_name, username=selected.username)
            target = User(telegram_id=20, first_name='Spammer', username='spam_name')
            session.add_all([chat, db_requester, db_selected, target])
            await session.flush()
            session.add_all([
                ChatModerator(chat_id=chat.id, user_id=db_requester.id, is_active=True),
                ChatModerator(chat_id=chat.id, user_id=db_selected.id, is_active=True),
                ModerationAction(chat_id=chat.id, target_user_id=target.id, moderator_user_id=db_selected.id, action=ModerationActionType.MUTE, reason='test'),
            ])
            await session.commit()
            await history(message, bot, session, config)

        result = message.answer.call_args.args[0]
        self.assertIn('Модератор:', result)
        self.assertIn('@selected_mod', result)
        self.assertNotIn('@requester_name', result)
        self.assertIn('@spam_name', result)
