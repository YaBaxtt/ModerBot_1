import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatPremiumAccess
from app.handlers.premium import buy_premium, premium_pre_checkout, premium_success
from app.services.premium import build_payload, chat_has_pro, parse_payload


class PremiumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = (Path(self.tmp.name) / 'test.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.factory() as session:
            chat = Chat(telegram_id=-1001, title='Customer group', username='customer_group')
            session.add(chat)
            await session.commit()
            self.chat_id = chat.id
        self.creator = SimpleNamespace(id=100, first_name='Creator', last_name=None, username='creator', is_bot=False)
        self.config = SimpleNamespace(is_owner=lambda _: False, premium_price_stars=50)
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='creator')),
            send_invoice=AsyncMock(),
            refund_star_payment=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    def test_payload_is_strict(self):
        payload = build_payload(self.chat_id, self.creator.id, 50)
        self.assertEqual(parse_payload(payload), (self.chat_id, self.creator.id, 50))
        self.assertIsNone(parse_payload('wrong:1:2:50'))
        self.assertIsNone(parse_payload('group_pro:-1:2:50'))

    async def test_invoice_uses_stars_and_exact_configured_price(self):
        callback = SimpleNamespace(
            data=f'premium:buy:{self.chat_id}',
            from_user=self.creator,
            answer=AsyncMock(),
        )
        async with self.factory() as session:
            await buy_premium(callback, session, self.bot, self.config)
        kwargs = self.bot.send_invoice.call_args.kwargs
        self.assertEqual(kwargs['chat_id'], self.creator.id)
        self.assertEqual(kwargs['currency'], 'XTR')
        self.assertEqual(kwargs['prices'][0].amount, 50)
        self.assertEqual(parse_payload(kwargs['payload']), (self.chat_id, self.creator.id, 50))
        self.assertNotIn('provider_token', kwargs)

    async def test_precheckout_validates_buyer_currency_and_price(self):
        query = SimpleNamespace(
            invoice_payload=build_payload(self.chat_id, self.creator.id, 50),
            currency='XTR',
            total_amount=50,
            from_user=self.creator,
            answer=AsyncMock(),
        )
        async with self.factory() as session:
            await premium_pre_checkout(query, session, self.bot, self.config)
        query.answer.assert_awaited_once_with(ok=True)

        query.answer.reset_mock()
        query.total_amount = 49
        async with self.factory() as session:
            await premium_pre_checkout(query, session, self.bot, self.config)
        self.assertFalse(query.answer.call_args.kwargs['ok'])

    async def test_successful_payment_grants_once_and_is_permanent(self):
        payment = SimpleNamespace(
            invoice_payload=build_payload(self.chat_id, self.creator.id, 50),
            currency='XTR',
            total_amount=50,
            telegram_payment_charge_id='charge-unique-1',
            provider_payment_charge_id='',
        )
        message = SimpleNamespace(from_user=self.creator, successful_payment=payment, answer=AsyncMock())
        async with self.factory() as session:
            await premium_success(message, session, self.bot, self.config)
            await session.commit()
            self.assertTrue(await chat_has_pro(session, self.chat_id, self.creator.id, self.config))
        async with self.factory() as session:
            await premium_success(message, session, self.bot, self.config)
            await session.commit()
            count = await session.scalar(select(func.count(ChatPremiumAccess.id)))
        self.assertEqual(count, 1)
        self.assertIn('уже обработан', message.answer.call_args.args[0])

    async def test_bot_owners_have_free_pro_without_purchase_row(self):
        owner_config = SimpleNamespace(is_owner=lambda user_id: user_id == self.creator.id)
        async with self.factory() as session:
            self.assertTrue(await chat_has_pro(session, self.chat_id, self.creator.id, owner_config))
            count = await session.scalar(select(func.count(ChatPremiumAccess.id)))
        self.assertEqual(count, 0)

    async def test_second_distinct_payment_is_refunded_instead_of_granting_twice(self):
        payment = SimpleNamespace(
            invoice_payload=build_payload(self.chat_id, self.creator.id, 50),
            currency='XTR', total_amount=50,
            telegram_payment_charge_id='charge-new', provider_payment_charge_id='',
        )
        message = SimpleNamespace(from_user=self.creator, successful_payment=payment, answer=AsyncMock())
        async with self.factory() as session:
            session.add(ChatPremiumAccess(
                chat_id=self.chat_id, purchased_by_user_id=None, price_stars=50,
                telegram_payment_charge_id='charge-old', provider_payment_charge_id=None,
            ))
            await session.commit()
            await premium_success(message, session, self.bot, self.config)
            await session.commit()
            count = await session.scalar(select(func.count(ChatPremiumAccess.id)))
        self.assertEqual(count, 1)
        self.bot.refund_star_payment.assert_awaited_once_with(user_id=self.creator.id, telegram_payment_charge_id='charge-new')
