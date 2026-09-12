import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import ChatMemberMember, ChatMemberLeft, User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Broadcast, BroadcastTarget, Chat, JoinVerification, ShopItem, ShopPurchase, User
from app.handlers.admin import commands
from app.handlers.broadcasts import BroadcastForm, create_and_dispatch, cancel
from app.handlers.leaderboard import build_top
from app.handlers.shop import buy
from app.handlers.verification import expire_after_delay, member_is_present
from app.middlewares.navigation import NavigationMiddleware
from app.services.health import inspect_chat
from app.services.shop import PurchaseError, create_purchase, refund_purchase
from app.services.broadcasts import scheduled_delivery


class ReviewFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'test.db'}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        async with self.factory() as session:
            self.user = User(telegram_id=7, first_name='Тест <&>', points=2000)
            self.chat = Chat(telegram_id=-1007, title='Тест')
            self.item = ShopItem(name='Подарок', price=800, stock=2)
            session.add_all([self.user, self.chat, self.item])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_purchase_replay_after_new_session_and_refund_once(self):
        async with self.factory() as session:
            purchase = await create_purchase(session, buyer_id=self.user.id, item_id=self.item.id, request_key='confirm:1')
            await session.commit()
            purchase_id = purchase.id
        async with self.factory() as session:
            with self.assertRaises(PurchaseError):
                await create_purchase(session, buyer_id=self.user.id, item_id=self.item.id, request_key='confirm:1')
            self.assertEqual((await session.get(User, self.user.id)).points, 1200)
            self.assertEqual(await session.scalar(select(func.count(ShopPurchase.id))), 1)
            self.assertIsNotNone(await refund_purchase(session, purchase_id))
            await session.commit()
        async with self.factory() as session:
            self.assertIsNone(await refund_purchase(session, purchase_id))
            self.assertEqual((await session.get(User, self.user.id)).points, 2000)
            self.assertEqual((await session.get(ShopItem, self.item.id)).stock, 2)

    async def test_purchase_notification_opens_buyer_profile(self):
        actor = TelegramUser(id=7, is_bot=False, first_name='Тест', username='gift_receiver')
        callback = SimpleNamespace(
            data=f'shop:buy:{self.item.id}',
            from_user=actor,
            message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=99, edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        bot = AsyncMock()
        async with self.factory() as session:
            await buy(callback, bot, session, SimpleNamespace(owner_ids=(999,)))
        owner_text = bot.send_message.call_args.args[1]
        markup = bot.send_message.call_args.kwargs['reply_markup']
        self.assertIn('tg://user?id=7', owner_text)
        self.assertIn('https://t.me/gift_receiver', owner_text)
        open_button = markup.inline_keyboard[0][0]
        self.assertEqual(open_button.text, '👤 Открыть покупателя')
        self.assertEqual(open_button.url, 'https://t.me/gift_receiver')

    async def test_insufficient_balance_does_not_reserve_stock(self):
        async with self.factory() as session:
            user = await session.get(User, self.user.id)
            user.points = 10
            await session.commit()
            with self.assertRaises(PurchaseError):
                await create_purchase(session, buyer_id=user.id, item_id=self.item.id)
            await session.commit()
        async with self.factory() as session:
            self.assertEqual((await session.get(ShopItem, self.item.id)).stock, 2)
            self.assertEqual((await session.get(User, self.user.id)).points, 10)

    async def test_unknown_group_never_gets_global_top(self):
        async with self.factory() as session:
            self.assertNotIn('Тест', await build_top(session, 'points', 'all', -999))

    async def test_cancel_is_consumed_before_fsm_text_handler(self):
        handler = AsyncMock()
        message = SimpleNamespace(text='/cancel', chat=SimpleNamespace(type='private'),
                                  from_user=SimpleNamespace(id=7), answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())
        await NavigationMiddleware()(handler, SimpleNamespace(callback_query=None, message=message),
                                     {'config': SimpleNamespace(is_owner=lambda _: True), 'state': state})
        state.clear.assert_awaited_once()
        handler.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_forged_owner_callback_cannot_reach_handler(self):
        callback = SimpleNamespace(data='admin:health', from_user=SimpleNamespace(id=9),
                                   message=SimpleNamespace(chat=SimpleNamespace(type='private')), answer=AsyncMock())
        handler = AsyncMock()
        await NavigationMiddleware()(handler, SimpleNamespace(callback_query=callback, message=None),
                                     {'config': SimpleNamespace(is_owner=lambda _: False)})
        handler.assert_not_awaited()
        callback.answer.assert_awaited_once()

    async def test_commands_only_owner(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=9), answer=AsyncMock(),
                                   message=SimpleNamespace(edit_text=AsyncMock()))
        await commands(callback, SimpleNamespace(is_owner=lambda _: False))
        callback.message.edit_text.assert_not_awaited()
        await commands(callback, SimpleNamespace(is_owner=lambda _: True))
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn('Для пользователей', text)
        self.assertIn('/unban', text)

    async def test_health_describes_missing_rights(self):
        bot = SimpleNamespace(id=42, get_chat_member=AsyncMock(return_value=SimpleNamespace(
            status='administrator', can_restrict_members=False, can_delete_messages=True)),
            get_chat=AsyncMock(return_value=SimpleNamespace(type='supergroup')))
        text = await inspect_chat(bot, self.chat)
        self.assertIn('✅ Удаление спама', text)
        self.assertIn('❌ Мут', text)

    async def test_normal_member_transition_is_detected(self):
        user = TelegramUser(id=7, is_bot=False, first_name='Тест')
        self.assertFalse(member_is_present(ChatMemberLeft(user=user)))
        self.assertTrue(member_is_present(ChatMemberMember(user=user)))

    async def test_finished_timeout_is_not_kicked_again(self):
        async with self.factory() as session:
            challenge = JoinVerification(chat_id=self.chat.id, user_id=self.user.id, question_key='founder',
                                         expires_at=datetime.now()-timedelta(minutes=5), is_verified=False,
                                         completed_at=datetime.now()-timedelta(minutes=4))
            session.add(challenge)
            await session.commit()
        bot = SimpleNamespace(ban_chat_member=AsyncMock(), get_chat_member=AsyncMock())
        await expire_after_delay(bot, self.factory, challenge.id, 0)
        bot.get_chat_member.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()

    async def test_scheduled_broadcast_sends_photo_button_and_persists_result(self):
        async with self.factory() as session:
            row = Broadcast(creator_user_id=self.user.id, target=BroadcastTarget.ALL_CHATS,
                            text='Текст', photo_file_id='photo', button_text='Открыть', button_url='https://example.com')
            session.add(row)
            await session.commit()
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        await scheduled_delivery(bot, self.factory, row.id)
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.call_args.args[0], self.chat.telegram_id)
        self.assertEqual(bot.send_photo.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url, 'https://example.com')
        async with self.factory() as session:
            saved = await session.get(Broadcast, row.id)
            self.assertIsNotNone(saved.sent_at)
            self.assertEqual(saved.delivered_count, 1)
        await scheduled_delivery(bot, self.factory, row.id)
        bot.send_photo.assert_awaited_once()

    async def test_dispatch_persists_before_queue_for_every_time(self):
        event = SimpleNamespace(from_user=TelegramUser(id=7, is_bot=False, first_name='Тест'), answer=AsyncMock())
        for scheduled in (None, datetime.now()+timedelta(minutes=10), datetime.now()+timedelta(hours=1)):
            state = SimpleNamespace(get_data=AsyncMock(return_value={'target': 'chats', 'text': 'Текст'}), clear=AsyncMock())
            with patch('app.handlers.broadcasts.schedule_delivery') as enqueue:
                async with self.factory() as session:
                    await create_and_dispatch(event, state, session, None, self.factory, scheduled)
                row_id = enqueue.call_args.args[2]
                async with self.factory() as session:
                    row = await session.get(Broadcast, row_id)
                    self.assertEqual(row.target, 'all_chats')
                    self.assertIsNotNone(row.scheduled_at)
            state.clear.assert_awaited_once()
