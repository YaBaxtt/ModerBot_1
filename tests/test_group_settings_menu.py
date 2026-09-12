import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, MemberEvent, ModerationAction, ModerationActionType, User, Warning
from app.handlers.common import group_settings, group_settings_detail
from app.handlers.group_controls import group_statistics


def telegram_error(chat_id: int) -> TelegramBadRequest:
    return TelegramBadRequest(method=GetChatMember(chat_id=chat_id, user_id=999), message='chat not found')


class GroupSettingsMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = (Path(self.tmp.name) / 'test.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.factory() as session:
            good = Chat(telegram_id=-1001, title='Working group')
            stale = Chat(telegram_id=-1002, title='Old group')
            session.add_all([good, stale])
            await session.commit()
            self.good_id, self.stale_id = good.id, stale.id
        self.config = SimpleNamespace(is_owner=lambda user_id: user_id == 10, premium_price_stars=50)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    def callback(self, data: str):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=10),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )

    async def test_stale_group_is_hidden_from_settings_list(self):
        async def get_member(chat_id, user_id):
            if chat_id == -1002:
                raise telegram_error(chat_id)
            return SimpleNamespace(status='administrator')

        bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=get_member))
        callback = self.callback('menu:group_settings')
        async with self.factory() as session:
            await group_settings(callback, session, bot, self.config)
        text = callback.message.edit_text.call_args.args[0]
        rows = callback.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        labels = [button.text for row in rows for button in row]
        self.assertIn('⚙️ Working group', labels)
        self.assertNotIn('⚙️ Old group', labels)
        self.assertIn('скрыто: <b>1</b>', text)

    async def test_failed_rights_check_opens_recovery_page_with_back_button(self):
        bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=telegram_error(-1002)))
        callback = self.callback(f'menu:group_settings:{self.stale_id}')
        async with self.factory() as session:
            await group_settings_detail(callback, session, bot, self.config)
        text = callback.message.edit_text.call_args.args[0]
        rows = callback.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertIn('ГРУППА НЕДОСТУПНА', text)
        self.assertEqual(rows[-1][0].callback_data, 'menu:group_settings')

    async def test_group_statistics_separates_periods_and_moderation(self):
        now = datetime.now(timezone.utc)
        async with self.factory() as session:
            target = User(telegram_id=77, first_name='Target')
            session.add(target)
            await session.flush()
            session.add_all([
                MemberEvent(chat_id=self.good_id, user_id=target.id, event_type='join', occurred_at=now - timedelta(hours=2)),
                MemberEvent(chat_id=self.good_id, user_id=target.id, event_type='leave', occurred_at=now - timedelta(days=3)),
                ModerationAction(chat_id=self.good_id, target_user_id=target.id, moderator_user_id=None, action=ModerationActionType.BAN, reason='test'),
                Warning(chat_id=self.good_id, target_user_id=target.id, moderator_user_id=None, reason='test'),
            ])
            await session.commit()
            bot = SimpleNamespace(get_chat_member_count=AsyncMock(return_value=42))
            callback = self.callback(f'groupcfg:stats:{self.good_id}')
            await group_statistics(callback, session, bot, self.config)
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn('Участников сейчас: <b>42</b>', text)
        self.assertIn('24 часа: <b>+1</b> / <b>−0</b>', text)
        self.assertIn('7 дней: <b>+1</b> / <b>−1</b>', text)
        self.assertIn('Банов: <b>1</b>', text)
        self.assertIn('Активных предупреждений: <b>1</b>', text)
