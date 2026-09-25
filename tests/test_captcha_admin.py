import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.models import Base, CaptchaQuestion
from app.handlers.captcha_admin import confirm_delete, toggle_automatic
from app.services.verification import auto_math_enabled, random_question, set_auto_math_enabled


class CaptchaAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = (Path(self.tmp.name) / 'captcha.db').as_posix()
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_automatic_math_is_enabled_by_default(self):
        async with self.factory() as session:
            self.assertTrue(await auto_math_enabled(session))
            question = await random_question(session)
            self.assertTrue(question.key.startswith('math_'))

    async def test_disabled_automatic_mode_uses_custom_question(self):
        async with self.factory() as session:
            session.add(CaptchaQuestion(
                question='Столица Франции?',
                options=['Париж', 'Рим', 'Берлин', 'Мадрид'],
                correct_index=0,
            ))
            await set_auto_math_enabled(session, False)
            await session.commit()
            question = await random_question(session)
            self.assertTrue(question.key.startswith('custom_'))
            self.assertEqual(question.options[question.correct_index], 'Париж')

    async def test_empty_custom_bank_falls_back_to_safe_math(self):
        async with self.factory() as session:
            await set_auto_math_enabled(session, False)
            await session.commit()
            question = await random_question(session)
            self.assertTrue(question.key.startswith('math_'))

    async def test_owner_cannot_disable_the_only_question_source(self):
        callback = SimpleNamespace(
            data='capadmin:auto:0', from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()),
        )
        config = SimpleNamespace(is_owner=lambda user_id: user_id == 1)
        async with self.factory() as session:
            await toggle_automatic(callback, session, config)
            self.assertTrue(await auto_math_enabled(session))
        callback.answer.assert_awaited_once_with(
            'Сначала добавьте хотя бы один свой вопрос.', show_alert=True,
        )

    async def test_delete_confirmation_parses_question_and_page(self):
        config = SimpleNamespace(is_owner=lambda user_id: user_id == 1)
        async with self.factory() as session:
            row = CaptchaQuestion(
                question='Удалить этот вопрос?', options=['Да', 'Нет', '1', '2'], correct_index=0,
            )
            session.add(row)
            await session.commit()
            callback = SimpleNamespace(
                data=f'capadmin:delete_confirm:{row.id}:3',
                from_user=SimpleNamespace(id=1), answer=AsyncMock(),
                message=SimpleNamespace(edit_text=AsyncMock()),
            )
            await confirm_delete(callback, session, config)
        callback.answer.assert_awaited_once_with()
        markup = callback.message.edit_text.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f'capadmin:delete:{row.id}:3')
