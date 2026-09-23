import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatProtectionSetting
from app.middlewares.premium_protection import PremiumProtectionMiddleware
from app.services.protections import set_forbidden_words, set_protection


def user(user_id: int, *, is_bot: bool = False):
    return SimpleNamespace(id=user_id, first_name=f'User {user_id}', last_name=None, username=f'user{user_id}', is_bot=is_bot)


class PremiumProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = (Path(self.tmp.name) / 'test.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.factory() as session:
            chat = Chat(telegram_id=-1001, title='Protected group')
            session.add(chat)
            await session.commit()
            self.chat_id = chat.id
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')),
            get_chat_administrators=AsyncMock(return_value=[]),
            delete_message=AsyncMock(), send_message=AsyncMock(),
            ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock(),
        )
        self.config = SimpleNamespace(is_owner=lambda _: False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    def message_event(self, **content):
        values = {field: None for field in ('photo', 'video', 'animation', 'audio', 'voice', 'video_note', 'document', 'sticker', 'story', 'poll', 'dice', 'game', 'contact', 'location', 'venue')}
        values.update(text=None, caption=None)
        values.update(content)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001, type='supergroup', title='Protected group', username='protected_group'), from_user=user(20),
            sender_chat=None, message_id=77, **values,
        )
        return SimpleNamespace(message=message, chat_member=None)

    async def test_sos_admin_sends_private_alerts_with_message_link_and_cooldown(self):
        self.bot.get_chat_administrators.return_value = [
            SimpleNamespace(user=user(501)),
            SimpleNamespace(user=user(502)),
            SimpleNamespace(user=user(503, is_bot=True)),
        ]
        handler = AsyncMock()
        event = self.message_event(text='@admin нужна помощь')
        middleware = PremiumProtectionMiddleware()
        async with self.factory() as session:
            # Reproduce a freshly started container whose monotonic clock has
            # not reached the 60-second cooldown duration yet.
            with patch('app.middlewares.premium_protection.monotonic', side_effect=(5.0, 6.0)):
                await middleware(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
                await middleware(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.bot.get_chat_administrators.assert_awaited_once_with(-1001)
        targets = [call.args[0] for call in self.bot.send_message.await_args_list]
        self.assertEqual(targets, [501, 502, -1001])
        private_markup = self.bot.send_message.await_args_list[0].kwargs['reply_markup']
        self.assertEqual(private_markup.inline_keyboard[0][0].url, 'https://t.me/protected_group/77')
        self.assertIn('ТРЕБУЕТСЯ АДМИНИСТРАТОР', self.bot.send_message.await_args_list[0].args[1])
        self.assertEqual(handler.await_count, 2)

    async def test_media_filter_deletes_media_and_stops_handlers(self):
        handler = AsyncMock()
        event = self.message_event(photo=[SimpleNamespace(file_id='photo')])
        async with self.factory() as session:
            await set_protection(session, self.chat_id, 'media_filter', True)
            await session.commit()
            middleware = PremiumProtectionMiddleware()
            await middleware(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.bot.delete_message.assert_awaited_once_with(-1001, 77)
        handler.assert_not_awaited()

    async def test_forbidden_word_filter_is_case_insensitive(self):
        handler = AsyncMock()
        event = self.message_event(text='This contains SCAM text')
        async with self.factory() as session:
            await set_forbidden_words(session, self.chat_id, ['scam'])
            await session.commit()
            await PremiumProtectionMiddleware()(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.bot.delete_message.assert_awaited_once()
        self.bot.ban_chat_member.assert_awaited_once_with(-1001, 20)
        handler.assert_not_awaited()

    async def test_legacy_forbidden_words_ban_even_bot_owner_or_chat_admin(self):
        handler = AsyncMock()
        event = self.message_event(text='ПОРНО')
        config = SimpleNamespace(is_owner=lambda _: True)
        self.bot.get_chat_member.return_value = SimpleNamespace(status='administrator')
        async with self.factory() as session:
            session.add(ChatProtectionSetting(
                chat_id=self.chat_id,
                key='forbidden_words',
                enabled=True,
                value='["порно", "хентай"]',
            ))
            await session.commit()
            await PremiumProtectionMiddleware()(handler, event, {'session': session, 'bot': self.bot, 'config': config})
        self.bot.delete_message.assert_awaited_once_with(-1001, 77)
        self.bot.ban_chat_member.assert_awaited_once_with(-1001, 20)
        handler.assert_not_awaited()

    async def test_porn_filter_uses_default_adult_dictionary(self):
        handler = AsyncMock()
        event = self.message_event(text='ссылка на hentai')
        async with self.factory() as session:
            await set_protection(session, self.chat_id, 'porn_filter', True)
            await session.commit()
            await PremiumProtectionMiddleware()(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.bot.delete_message.assert_awaited_once_with(-1001, 77)
        self.bot.ban_chat_member.assert_awaited_once_with(-1001, 20)
        handler.assert_not_awaited()

    async def test_four_joins_inside_two_seconds_are_kicked(self):
        middleware = PremiumProtectionMiddleware()
        handler = AsyncMock()
        async with self.factory() as session:
            await set_protection(session, self.chat_id, 'raid_guard', True)
            await session.commit()
            for user_id in range(30, 34):
                event = SimpleNamespace(
                    message=None,
                    chat_member=SimpleNamespace(
                        chat=SimpleNamespace(id=-1001, type='supergroup'),
                        old_chat_member=SimpleNamespace(status='left', is_member=False),
                        new_chat_member=SimpleNamespace(status='member', is_member=True, user=user(user_id)),
                    ),
                )
                await middleware(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.assertEqual(self.bot.ban_chat_member.await_count, 4)
        self.assertEqual(self.bot.unban_chat_member.await_count, 4)
        self.assertEqual(handler.await_count, 3)

    async def test_bot_join_is_permanently_banned(self):
        handler = AsyncMock()
        event = SimpleNamespace(
            message=None,
            chat_member=SimpleNamespace(
                chat=SimpleNamespace(id=-1001, type='supergroup'),
                old_chat_member=SimpleNamespace(status='left', is_member=False),
                new_chat_member=SimpleNamespace(status='member', is_member=True, user=user(99, is_bot=True)),
            ),
        )
        async with self.factory() as session:
            await set_protection(session, self.chat_id, 'block_bots', True)
            await session.commit()
            await PremiumProtectionMiddleware()(handler, event, {'session': session, 'bot': self.bot, 'config': self.config})
        self.bot.ban_chat_member.assert_awaited_once_with(-1001, 99)
        self.bot.unban_chat_member.assert_not_awaited()
        handler.assert_not_awaited()
