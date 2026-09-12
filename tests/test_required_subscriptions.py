import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatProtectionSetting, RequiredSubscription, Setting, User
from app.middlewares.required_subscription import RequiredSubscriptionMiddleware
from app.services.subscriptions import record_check, subscription_stats


def telegram_user(user_id: int):
    return SimpleNamespace(id=user_id, first_name=f'User {user_id}', last_name=None, username=f'user{user_id}', is_bot=False)


class RequiredSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = (Path(self.tmp.name) / 'test.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.config = SimpleNamespace(is_owner=lambda _: False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_private_gate_blocks_until_user_subscribes_and_records_stats(self):
        async with self.factory() as session:
            session.add(Setting(key='required_subscription_private_enabled', value='1'))
            link = RequiredSubscription(
                scope='private', owner_chat_id=None, target_telegram_id=-2001,
                title='News', description='Новости проекта', url='https://t.me/news',
            )
            session.add(link)
            await session.commit()
            actor = telegram_user(42)
            message = SimpleNamespace(
                chat=SimpleNamespace(id=42, type='private'), from_user=actor,
                answer=AsyncMock(), message_id=5,
            )
            event = SimpleNamespace(message=message, callback_query=None)
            bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='left')), id=999)
            handler = AsyncMock()
            middleware = RequiredSubscriptionMiddleware()
            await middleware(handler, event, {'session': session, 'bot': bot, 'config': self.config, 'raw_state': None})
            handler.assert_not_awaited()
            message.answer.assert_awaited_once()
            markup = message.answer.call_args.kwargs['reply_markup']
            self.assertEqual(markup.inline_keyboard[0][0].url, 'https://t.me/news')
            self.assertEqual(markup.inline_keyboard[-1][0].style, 'success')
            stats = await subscription_stats(session, link.id)
            self.assertEqual(stats, {'impressions': 1, 'users': 1, 'checks': 0, 'passed': 0})

            user = await session.scalar(select(User).where(User.telegram_id == 42))
            await record_check(session, [link], [], user)
            await session.commit()
            stats = await subscription_stats(session, link.id)
            self.assertEqual(stats, {'impressions': 1, 'users': 1, 'checks': 1, 'passed': 1})

            bot.get_chat_member.return_value = SimpleNamespace(status='member')
            await middleware(handler, event, {'session': session, 'bot': bot, 'config': self.config, 'raw_state': None})
            handler.assert_awaited_once()

    async def test_group_gate_deletes_message_and_uses_user_bound_check_button(self):
        async with self.factory() as session:
            chat = Chat(telegram_id=-1001, title='Group')
            session.add(chat)
            await session.flush()
            session.add(ChatProtectionSetting(chat_id=chat.id, key='required_subscription', enabled=True))
            session.add(RequiredSubscription(
                scope='group', owner_chat_id=chat.id, target_telegram_id=-2002,
                title='Sponsor', url='https://t.me/sponsor',
            ))
            await session.commit()
            actor = telegram_user(77)
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-1001, type='supergroup'), from_user=actor,
                message_id=99,
            )
            event = SimpleNamespace(message=message, callback_query=None)

            async def membership(chat_id, user_id):
                return SimpleNamespace(status='member' if chat_id == -1001 else 'left')

            bot = SimpleNamespace(
                id=999, get_chat_member=AsyncMock(side_effect=membership),
                delete_message=AsyncMock(), send_message=AsyncMock(),
            )
            handler = AsyncMock()
            await RequiredSubscriptionMiddleware()(handler, event, {'session': session, 'bot': bot, 'config': self.config, 'raw_state': None})
            handler.assert_not_awaited()
            bot.delete_message.assert_awaited_once_with(-1001, 99)
            markup = bot.send_message.call_args.kwargs['reply_markup']
            self.assertEqual(markup.inline_keyboard[-1][0].callback_data, f'subcheck:group:{chat.id}:77')

    async def test_group_administrators_are_not_blocked(self):
        async with self.factory() as session:
            chat = Chat(telegram_id=-1001, title='Group')
            session.add(chat)
            await session.flush()
            session.add(ChatProtectionSetting(chat_id=chat.id, key='required_subscription', enabled=True))
            session.add(RequiredSubscription(scope='group', owner_chat_id=chat.id, target_telegram_id=-2002, title='Sponsor', url='https://t.me/sponsor'))
            await session.commit()
            actor = telegram_user(88)
            message = SimpleNamespace(chat=SimpleNamespace(id=-1001, type='supergroup'), from_user=actor, message_id=100)
            event = SimpleNamespace(message=message, callback_query=None)
            bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')))
            handler = AsyncMock()
            await RequiredSubscriptionMiddleware()(handler, event, {'session': session, 'bot': bot, 'config': self.config, 'raw_state': None})
            handler.assert_awaited_once()

    async def test_successful_star_payment_is_never_blocked_by_private_gate(self):
        async with self.factory() as session:
            session.add(Setting(key='required_subscription_private_enabled', value='1'))
            session.add(RequiredSubscription(scope='private', target_telegram_id=-2001, title='News', url='https://t.me/news'))
            await session.commit()
            message = SimpleNamespace(
                chat=SimpleNamespace(id=42, type='private'), from_user=telegram_user(42),
                successful_payment=SimpleNamespace(total_amount=50),
            )
            event = SimpleNamespace(message=message, callback_query=None, pre_checkout_query=None)
            bot = SimpleNamespace(id=999)
            handler = AsyncMock()
            await RequiredSubscriptionMiddleware()(handler, event, {'session': session, 'bot': bot, 'config': self.config, 'raw_state': None})
            handler.assert_awaited_once()
