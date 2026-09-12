import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ModerationAction, ModerationActionType, User
from app.handlers.moderator_panel import moderator_detail, remember_bot_chat
from app.services.moderators import can_manage_chat, can_moderate_chat, revoke_moderator, set_moderator


class ModeratorPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'test.db').as_posix())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.factory() as session:
            self.chat = Chat(telegram_id=-1001, title='Owned group')
            self.other_chat = Chat(telegram_id=-1002, title='Other group')
            self.owner = User(telegram_id=10, first_name='Owner')
            self.moderator = User(telegram_id=20, first_name='Moderator', username='trusted_mod')
            self.target = User(telegram_id=30, first_name='Target')
            session.add_all([self.chat, self.other_chat, self.owner, self.moderator, self.target])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    def bot(self):
        async def get_member(chat_id, user_id):
            if chat_id == -1001 and user_id == 10:
                return SimpleNamespace(status='creator')
            return SimpleNamespace(status='member' if chat_id == -1001 else 'administrator')
        return SimpleNamespace(get_chat_member=AsyncMock(side_effect=get_member), send_message=AsyncMock())

    async def test_only_creator_or_explicit_assignment_can_moderate_chat(self):
        bot = self.bot()
        config = SimpleNamespace(is_owner=lambda _: False)
        async with self.factory() as session:
            self.assertTrue(await can_manage_chat(bot, self.chat, 10, config))
            # Being a normal Telegram administrator is deliberately insufficient.
            self.assertFalse(await can_moderate_chat(bot, session, self.other_chat, 20, config))
            await set_moderator(session, chat_id=self.chat.id, user_id=self.moderator.id, granted_by_user_id=self.owner.id)
            await session.commit()
            self.assertTrue(await can_moderate_chat(bot, session, self.chat, 20, config))
            self.assertFalse(await can_moderate_chat(bot, session, self.other_chat, 20, config))
            self.assertTrue(await revoke_moderator(session, chat_id=self.chat.id, user_id=self.moderator.id))
            await session.commit()
            self.assertFalse(await can_moderate_chat(bot, session, self.chat, 20, config))

    async def test_bot_membership_event_registers_and_deactivates_group(self):
        telegram_chat = SimpleNamespace(id=-2001, type='supergroup', title='New group', full_name=None, username='new_group')
        async with self.factory() as session:
            await remember_bot_chat(SimpleNamespace(chat=telegram_chat, new_chat_member=SimpleNamespace(status='administrator')), session)
            await session.commit()
            chat = await session.scalar(select(Chat).where(Chat.telegram_id == -2001))
            self.assertTrue(chat.is_active)
            await remember_bot_chat(SimpleNamespace(chat=telegram_chat, new_chat_member=SimpleNamespace(status='left')), session)
            await session.commit()
            self.assertFalse(chat.is_active)

    async def test_moderator_card_shows_counts_and_only_last_five_actions(self):
        bot = self.bot()
        config = SimpleNamespace(is_owner=lambda _: False)
        async with self.factory() as session:
            await set_moderator(session, chat_id=self.chat.id, user_id=self.moderator.id, granted_by_user_id=self.owner.id)
            for index in range(6):
                session.add(ModerationAction(
                    chat_id=self.chat.id,
                    target_user_id=self.target.id,
                    moderator_user_id=self.moderator.id,
                    action=ModerationActionType.BAN if index < 4 else ModerationActionType.MUTE,
                    reason=f'action-{index}',
                    created_at=datetime.now() + timedelta(minutes=index),
                ))
            await session.commit()
            callback = SimpleNamespace(
                data=f'moder:view:{self.chat.id}:{self.moderator.id}',
                from_user=SimpleNamespace(id=10),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            await moderator_detail(callback, session, bot, config)
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn('Баны: <b>4</b>', text)
        self.assertIn('Муты: <b>2</b>', text)
        self.assertNotIn('action-0', text)
        for index in range(1, 6):
            self.assertIn(f'action-{index}', text)
        buttons = callback.message.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, f'moder:remove:{self.chat.id}:{self.moderator.id}')
