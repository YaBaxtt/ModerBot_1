import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiogram.types import Chat as TelegramChat
from aiogram.types import Message, User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import ActivityMessage, Base, ShopItem, User
from app.services.activity import record_message_activity
from app.services.shop import PurchaseError, create_purchase
from app.services.users import upsert_chat, upsert_user


class SQLiteIntegrationTests(unittest.TestCase):
    def test_upsert_activity_and_purchase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(self._scenario(Path(directory) / "test.db"))

    async def _scenario(self, path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        user_data = TelegramUser(id=42, is_bot=False, first_name="Тест", username="test_user")
        chat_data = TelegramChat(id=-42, type="supergroup", title="Тестовый чат")

        async with factory() as session:
            user = await upsert_user(session, user_data)
            chat = await upsert_chat(session, chat_data)
            message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat_data, from_user=user_data, text="Тест")
            await record_message_activity(session, message)
            await record_message_activity(session, message)  # duplicate update must not award twice
            item = ShopItem(name="Тестовый товар", price=1, stock=1)
            session.add(item)
            await session.flush()
            purchase = await create_purchase(session, buyer_id=user.id, item_id=item.id)
            self.assertEqual(purchase.price_paid, 1)
            with self.assertRaises(PurchaseError):
                await create_purchase(session, buyer_id=user.id, item_id=item.id)
            await session.commit()

        async with factory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 42))
            markers = (await session.scalars(select(ActivityMessage))).all()
            self.assertEqual(user.message_count, 1)
            self.assertEqual(user.points, 0)
            self.assertEqual(len(markers), 1)
        await engine.dispose()

