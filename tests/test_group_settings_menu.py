import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatModerator, ChatProtectionSetting, MemberEvent, ModerationAction, ModerationActionType, User, Warning
from app.handlers.common import group_settings, group_settings_detail
from app.handlers.group_controls import group_statistics, render_protection
from app.services.protections import set_protection_action


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

    async def test_assigned_moderator_can_open_group_settings(self):
        async with self.factory() as session:
            moderator = User(telegram_id=20, first_name='Moderator')
            session.add(moderator)
            await session.flush()
            session.add(ChatModerator(chat_id=self.good_id, user_id=moderator.id, granted_by_user_id=None))
            await session.commit()

        async def get_member(chat_id, user_id):
            if user_id == 999:
                return SimpleNamespace(
                    status='administrator', can_delete_messages=True,
                    can_restrict_members=True, can_pin_messages=True,
                )
            return SimpleNamespace(status='member')

        bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=get_member))
        callback = SimpleNamespace(
            data=f'menu:group_settings:{self.good_id}',
            from_user=SimpleNamespace(id=20), answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        async with self.factory() as session:
            await group_settings_detail(callback, session, bot, self.config)
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn('ПАРАМЕТРЫ ГРУППЫ', text)
        rows = callback.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertTrue(any(button.callback_data == f'groupcfg:view:captcha:{self.good_id}' for row in rows for button in row))

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

    async def test_antispam_menu_has_exactly_three_punishments(self):
        target = SimpleNamespace(edit_text=AsyncMock())
        async with self.factory() as session:
            chat = await session.get(Chat, self.good_id)
            await set_protection_action(session, chat.id, 'antispam', 'warn')
            await session.commit()
            await render_protection(target, session, chat, 'antispam')
        text = target.edit_text.call_args.args[0]
        rows = target.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        actions = [button for row in rows for button in row if button.callback_data and button.callback_data.startswith('groupcfg:action:antispam:')]
        self.assertEqual([button.text for button in actions], ['⚠️ Дать варн', '🔇 Дать мут', '🚫 Дать бан'])
        self.assertEqual(sum(button.style == 'success' for button in actions), 1)
        self.assertIn('4 любых сообщения за 1 секунду', text)
        self.assertIn('4 одинаковых сообщения, стикера или GIF за 5 секунд', text)

    async def test_antispam_rejects_hidden_delete_action(self):
        async with self.factory() as session:
            with self.assertRaises(KeyError):
                await set_protection_action(session, self.good_id, 'antispam', 'delete')

    async def test_antispam_ignores_legacy_invalid_saved_action(self):
        async with self.factory() as session:
            row = ChatProtectionSetting(chat_id=self.good_id, key='antispam', enabled=True, value='{"action":"delete"}')
            session.add(row)
            await session.commit()
            chat = await session.get(Chat, self.good_id)
            target = SimpleNamespace(edit_text=AsyncMock())
            await render_protection(target, session, chat, 'antispam')
        text = target.edit_text.call_args.args[0]
        self.assertIn('<b>Наказание:</b> мут на 1 час', text)
