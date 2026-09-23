import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Message, Update, User as TelegramUser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Chat, ChatModerator, PrivateReport, Report, ReportStatus, Setting, User
from app.handlers.announcements import publish_announcement
from app.handlers.private_reports import deliver_private_report, evidence
from app.handlers.reports import report as group_report, review_report
from app.handlers.verification import send_private_welcome, question_keyboard
from app.middlewares.database import DatabaseMiddleware
from app.middlewares.navigation import NavigationMiddleware
from app.middlewares.private_navigation import PrivateNavigationMiddleware, navigation_context
from app.middlewares.command_cleanup import CommandCleanupMiddleware
from app.handlers.private_reports import target_is_protected
from app.handlers.admin import MenuLinkForm, menu_link_save
from app.handlers.common import menu_links
from app.handlers.fallbacks import stale_private_button
from app.services.verification import QUESTIONS, load_questions


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = method.__api_method__
        if name == 'getMe':
            return TelegramUser(id=bot.id, is_bot=True, first_name='Bot', username='test_bot')
        if name == 'sendMediaGroup':
            return [Message(message_id=len(self.calls) + 100 + index, date=datetime.now(timezone.utc), chat={'id': method.chat_id, 'type': 'private'}, from_user={'id': bot.id, 'is_bot': True, 'first_name': 'Bot'}, caption=getattr(media, 'caption', None)) for index, media in enumerate(method.media)]
        if name.startswith('send') or name == 'editMessageText':
            return Message(message_id=len(self.calls) + 100, date=datetime.now(timezone.utc), chat={'id': method.chat_id, 'type': 'private'}, from_user={'id': bot.id, 'is_bot': True, 'first_name': 'Bot'}, text=getattr(method, 'text', None) or 'media')
        return True


class PrivateWorkflows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'test.db').as_posix())
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_navigation_scopes_private_current_task_only(self):
        middleware, sender = PrivateNavigationMiddleware(), AsyncMock()
        token = navigation_context.set((asyncio.current_task(), 123, 'nav:private_main'))
        try:
            await middleware(sender, None, SendMessage(chat_id=123, text='done'))
            sent = sender.call_args.args[1]
            self.assertEqual(sent.reply_markup.inline_keyboard[-1][0].callback_data, 'nav:private_main')
            await middleware(sender, None, SendMessage(chat_id=-100, text='group'))
            self.assertIsNone(sender.call_args.args[1].reply_markup)
            await asyncio.create_task(middleware(sender, None, SendMessage(chat_id=123, text='broadcast')))
            self.assertIsNone(sender.call_args.args[1].reply_markup)
        finally:
            navigation_context.reset(token)

    async def test_expired_callback_ack_does_not_block_navigation(self):
        middleware = PrivateNavigationMiddleware()
        method = AnswerCallbackQuery(callback_query_id='expired')

        async def expired_sender(bot, request):
            raise TelegramBadRequest(method=request, message='Bad Request: query is too old and response timeout expired or query ID is invalid')

        self.assertTrue(await middleware(expired_sender, None, method))

        async def other_bad_request(bot, request):
            raise TelegramBadRequest(method=request, message='Bad Request: unrelated failure')

        with self.assertRaises(TelegramBadRequest):
            await middleware(other_bad_request, None, method)

    async def test_button_from_interrupted_form_recovers_to_main_menu(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=1, chat_id=111, user_id=111))
        await state.set_state('AdForm:placement')
        bot = AsyncMock()
        bot.get_me.return_value = SimpleNamespace(username='test_bot')
        callback = SimpleNamespace(
            data='ads:placement:999',
            from_user=SimpleNamespace(id=111),
            bot=bot,
            answer=AsyncMock(),
            message=SimpleNamespace(text='Старая форма', edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), answer=AsyncMock()),
        )
        config = SimpleNamespace(is_owner=lambda uid: False, community_group_url=None, community_channel_url=None)
        async with self.factory() as session:
            await stale_private_button(callback, state, session, config)
        self.assertIsNone(await state.get_state())
        self.assertIn('NIK MODER BOT', callback.message.edit_text.call_args.args[0])
        markup = callback.message.edit_text.call_args.kwargs['reply_markup']
        self.assertTrue(any(button.callback_data == 'ads:start' for row in markup.inline_keyboard for button in row))
        await storage.close()

    async def test_slash_commands_are_removed_from_groups_before_handling(self):
        events = []
        received = {}
        async def handler(_, data):
            received.update(data)
            events.append('handler')
            return 'handled'
        async def delete():
            events.append('delete')
        message = SimpleNamespace(chat=SimpleNamespace(type='supergroup', id=-100), text='  /help', caption=None, delete=delete)
        result = await CommandCleanupMiddleware()(handler, SimpleNamespace(message=message), {})
        self.assertEqual(result, 'handled')
        self.assertEqual(events, ['delete', 'handler'])
        self.assertTrue(received['group_command_deleted'])

    async def test_known_owner_cannot_be_private_report_target(self):
        from app.database.models import User
        async with self.factory() as db:
            db.add(User(telegram_id=999, username='owner_name', first_name='Owner'))
            await db.commit()
            protected = await target_is_protected('OWNER_NAME', db, AsyncMock(), SimpleNamespace(owner_ids=(999,)))
        self.assertTrue(protected)

    async def test_owner_can_configure_working_group_and_channel_links(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=1, chat_id=999, user_id=999))
        config = SimpleNamespace(owner_ids=(999,), is_owner=lambda uid: uid == 999, community_group_url=None, community_channel_url=None)
        async with self.factory() as db:
            for kind, url in [('group', 't.me/test_group'), ('channel', '@test_channel')]:
                await state.set_state(MenuLinkForm.url)
                await state.set_data({'menu_link_kind': kind})
                message = SimpleNamespace(text=url, from_user=SimpleNamespace(id=999), answer=AsyncMock())
                await menu_link_save(message, state, db, config)
            self.assertEqual(await menu_links(db, config), ('https://t.me/test_group', 'https://t.me/test_channel'))
        await storage.close()

    async def test_real_dispatcher_report_media_cancel_back_and_owner_review(self):
        from app.handlers.private_reports import router
        from app.handlers.reports import router as group_reports_router
        from app.handlers.common import router as common_router
        from app.handlers.advertising import router as ads_router
        session = RecordingSession()
        bot = Bot('123456:TESTTOKEN', session=session, default=DefaultBotProperties(parse_mode='HTML'))
        bot.session.middleware(PrivateNavigationMiddleware())
        dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        dp['config'] = SimpleNamespace(owner_ids=(999,), is_owner=lambda uid: uid == 999)
        dp.update.outer_middleware(NavigationMiddleware())
        dp.update.outer_middleware(DatabaseMiddleware(self.factory))
        dp.include_routers(common_router, router, group_reports_router, ads_router)
        seq = 0

        async def send(text=None, callback=None, uid=111, **media):
            nonlocal seq
            seq += 1
            actor = {'id': uid, 'is_bot': False, 'first_name': 'SECRET_REPORTER', 'username': 'secret_author'}
            msg = {'message_id': seq, 'date': datetime.now(timezone.utc), 'chat': {'id': uid, 'type': 'private'}, 'from_user': actor, **media}
            if callback:
                msg.update(text='previous screen', from_user={'id': bot.id, 'is_bot': True, 'first_name': 'Bot'})
                update = Update(update_id=seq, callback_query={'id': str(seq), 'from_user': actor, 'chat_instance': 'abc', 'data': callback, 'message': msg})
            else:
                if text is not None: msg['text'] = text
                update = Update(update_id=seq, message=msg)
            await dp.feed_update(bot, update)

        try:
            await send('/report @target_user')
            await send('Обещал товар, получил оплату и исчез')
            await send(photo=[{'file_id': 'photo1', 'file_unique_id': 'p1', 'width': 100, 'height': 100}], media_group_id='album1')
            await send(video={'file_id': 'video1', 'file_unique_id': 'v1', 'width': 100, 'height': 100, 'duration': 1}, media_group_id='album1')
            await send('/cancel')
            await send(callback='private_report:send')
            await send(callback='private_report:send')  # stale confirmation must not duplicate
            async with self.factory() as db:
                self.assertEqual(await db.scalar(select(func.count(PrivateReport.id))), 1)
                row = await db.scalar(select(PrivateReport))
                self.assertEqual([item['type'] for item in row.evidence], ['photo', 'video'])
                report_id = row.id
            owner_calls = [m for m in session.calls if getattr(m, 'chat_id', None) == 999 and m.__api_method__.startswith('send')]
            self.assertEqual([m.__api_method__ for m in owner_calls], ['sendMediaGroup', 'sendMessage'])
            album_caption = owner_calls[0].media[0].caption
            self.assertIn('SECRET_REPORTER', album_caption)
            self.assertIn('@secret_author', album_caption)
            self.assertIn('<code>111</code>', album_caption)
            self.assertIn('tg://user?id=111', album_caption)
            confirmation = next(m for m in session.calls if 'сохранена для рассмотрения' in (getattr(m, 'text', '') or ''))
            self.assertTrue(confirmation.reply_markup)
            await send(callback=f'private_report:review:{report_id}', uid=999)
            async with self.factory() as db:
                self.assertEqual((await db.get(PrivateReport, report_id)).status, 'reviewed')
            self.assertTrue(any('рассмотрена' in (getattr(m, 'text', '') or '') and m.chat_id == 111 for m in session.calls))
            # Exiting a form must discard stale FSM state before the next input.
            await send('/report @someone_else')
            await send(callback='nav:private_main')
            await send('This should not become a reason')
            state = dp.fsm.get_context(bot=bot, chat_id=111, user_id=111)
            self.assertIsNone(await state.get_state())
            await send(callback='menu:profile')
            profile_screen = next(m for m in reversed(session.calls) if 'МОЙ ПРОФИЛЬ' in (getattr(m, 'text', '') or ''))
            self.assertIn('ПРОГРЕСС', profile_screen.text)
            self.assertIn('Серия наград', profile_screen.text)
            # Advertising flow: formerly missing final navigation.
            await send(callback='ads:start')
            await send('Мой проект')
            await send('https://example.com')
            await send(callback='ads:placement:1')
            await send(callback='ads:format:Пост')
            await send('1000')
            await send(callback='ads:skip_comment')
            await send(callback='ads:send')
            ad_final = next(m for m in session.calls if 'Мы свяжемся с вами здесь' in (getattr(m, 'text', '') or ''))
            self.assertEqual(ad_final.reply_markup.inline_keyboard[-1][0].callback_data, 'nav:private_main')

            # Real router order must not let the private /report workflow
            # swallow a group complaint before it reaches the group handler.
            async with self.factory() as db:
                chat = Chat(telegram_id=-100, title='Group')
                moderator = User(telegram_id=333, first_name='Moderator')
                db.add_all([chat, moderator])
                await db.flush()
                db.add(ChatModerator(chat_id=chat.id, user_id=moderator.id, is_active=True))
                await db.commit()
            seq += 1
            group_message = {
                'message_id': seq,
                'date': datetime.now(timezone.utc),
                'chat': {'id': -100, 'type': 'supergroup', 'title': 'Group'},
                'from_user': {'id': 111, 'is_bot': False, 'first_name': 'Reporter'},
                'text': '/report флуд',
                'reply_to_message': {
                    'message_id': seq - 1,
                    'date': datetime.now(timezone.utc),
                    'chat': {'id': -100, 'type': 'supergroup', 'title': 'Group'},
                    'from_user': {'id': 222, 'is_bot': False, 'first_name': 'Target'},
                    'text': 'spam',
                },
            }
            await dp.feed_update(bot, Update(update_id=seq, message=group_message))
            delivered = [m for m in session.calls if getattr(m, '__api_method__', '') == 'sendMessage' and getattr(m, 'chat_id', None) == 333]
            self.assertTrue(any('Новая жалоба' in (m.text or '') for m in delivered))
        finally:
            await dp.fsm.close()
            await bot.session.close()

    async def test_evidence_is_attached_to_complaint_without_forwarding(self):
        bot = AsyncMock()
        row = SimpleNamespace(id=1, target_username='target', reason='<test>', evidence=[{'type': 'photo', 'file_id': 'file'}])
        reporter = SimpleNamespace(telegram_id=111, first_name='Reporter', username=None)
        await deliver_private_report(bot, 999, row, reporter)
        bot.forward_message.assert_not_called()
        self.assertEqual(bot.send_photo.call_args.args, (999, 'file'))
        self.assertIn('&lt;test&gt;', bot.send_photo.call_args.kwargs['caption'])
        self.assertIn('tg://user?id=111', bot.send_photo.call_args.kwargs['caption'])
        bot.send_message.assert_not_called()

    async def test_multiple_evidence_files_are_one_album_with_report_caption(self):
        bot = AsyncMock()
        row = SimpleNamespace(id=2, target_username='target', reason='Описание', evidence=[
            {'type': 'photo', 'file_id': 'photo'},
            {'type': 'video', 'file_id': 'video'},
        ])
        reporter = SimpleNamespace(telegram_id=111, first_name='Reporter', last_name=None, username='reporter')
        await deliver_private_report(bot, 999, row, reporter)
        media = bot.send_media_group.call_args.kwargs['media']
        self.assertEqual([item.media for item in media], ['photo', 'video'])
        self.assertIn('Личная жалоба #2', media[0].caption)
        self.assertIn('Описание', media[0].caption)
        self.assertIsNone(media[1].caption)
        bot.send_photo.assert_not_called()
        bot.send_video.assert_not_called()
        self.assertIn('Действия по жалобе #2', bot.send_message.call_args.args[1])
        self.assertTrue(bot.send_message.call_args.kwargs['reply_markup'])

    async def test_evidence_limit_and_album_receipt_does_not_spam(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=1, chat_id=2, user_id=2))
        message = SimpleNamespace(photo=[SimpleNamespace(file_id='photo')], media_group_id='album', answer=AsyncMock())
        for _ in range(12):
            await evidence(message, state)
        self.assertEqual(len((await state.get_data())['evidence']), 10)
        # One album receipt, one overflow warning; no receipt for every file.
        self.assertEqual(message.answer.await_count, 2)
        await storage.close()

    async def test_group_report_deleted_first_and_acknowledged_privately(self):
        api = RecordingSession()
        bot = Bot('123456:TESTTOKEN', session=api)
        raw = {'message_id': 2, 'date': datetime.now(timezone.utc), 'chat': {'id': -100, 'type': 'supergroup', 'title': 'Group'}, 'from_user': {'id': 111, 'is_bot': False, 'first_name': 'SECRET_REPORTER'}, 'text': '/report флуд'}
        raw['reply_to_message'] = {**raw, 'message_id': 1, 'text': 'spam', 'from_user': {'id': 222, 'is_bot': False, 'first_name': 'Target'}}
        message = Message.model_validate(raw).as_(bot)
        async with self.factory() as db:
            chat = Chat(telegram_id=-100, title='Group')
            moderator = User(telegram_id=333, first_name='Moderator')
            db.add_all([chat, moderator])
            await db.flush()
            db.add(ChatModerator(chat_id=chat.id, user_id=moderator.id, is_active=True))
            await db.commit()
            await group_report(message, bot, db, SimpleNamespace(owner_ids=(999,)))
            self.assertEqual(await db.scalar(select(func.count(Report.id))), 1)
        self.assertEqual(api.calls[0].__api_method__, 'deleteMessage')
        group_messages = [m for m in api.calls if m.__api_method__ == 'sendMessage' and m.chat_id == -100]
        self.assertEqual(len(group_messages), 1)
        self.assertEqual(group_messages[0].ephemeral_message_parameters.receiver_user_id, 111)
        moderator_text = next(m.text for m in api.calls if m.__api_method__ == 'sendMessage' and m.chat_id == 333)
        self.assertIn('SECRET_REPORTER', moderator_text)
        self.assertIn('tg://user?id=111', moderator_text)
        self.assertFalse(any(m.__api_method__ == 'sendMessage' and m.chat_id == 999 for m in api.calls))
        await bot.session.close()

    async def test_only_assigned_group_moderator_can_process_group_report(self):
        async with self.factory() as db:
            chat = Chat(telegram_id=-100, title='Group')
            moderator = User(telegram_id=333, first_name='Moderator')
            reporter = User(telegram_id=111, first_name='Reporter')
            target = User(telegram_id=222, first_name='Target')
            db.add_all([chat, moderator, reporter, target])
            await db.flush()
            db.add(ChatModerator(chat_id=chat.id, user_id=moderator.id, is_active=True))
            row = Report(chat_id=chat.id, reporter_user_id=reporter.id, target_user_id=target.id, reason='spam')
            db.add(row)
            await db.commit()

            denied = SimpleNamespace(
                data=f'report:review:{row.id}',
                from_user=SimpleNamespace(id=444, username=None, first_name='Outsider', last_name=None),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            )
            config = SimpleNamespace(is_owner=lambda _: False)
            await review_report(denied, db, config)
            self.assertEqual(row.status, ReportStatus.OPEN)
            denied.answer.assert_awaited_once_with(
                'Эту жалобу может обработать только назначенный модератор этой группы.',
                show_alert=True,
            )

            allowed = SimpleNamespace(
                data=f'report:review:{row.id}',
                from_user=SimpleNamespace(id=333, username=None, first_name='Moderator', last_name=None),
                answer=AsyncMock(),
                message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            )
            await review_report(allowed, db, config)
            self.assertEqual(row.status, ReportStatus.REVIEWED)
            self.assertEqual(row.reviewed_by_user_id, moderator.id)

    async def test_failed_group_command_deletion_warns_privately(self):
        class CannotDelete(RecordingSession):
            async def make_request(self, bot, method, timeout=None):
                if method.__api_method__ == 'deleteMessage':
                    self.calls.append(method)
                    raise TelegramBadRequest(method=method, message='not enough rights')
                return await super().make_request(bot, method, timeout)
        api = CannotDelete()
        bot = Bot('123456:TESTTOKEN', session=api)
        message = Message(message_id=2, date=datetime.now(timezone.utc), chat={'id': -100, 'type': 'supergroup', 'title': 'Group'}, from_user={'id': 111, 'is_bot': False, 'first_name': 'Reporter'}, text='/report').as_(bot)
        async with self.factory() as db:
            await group_report(message, bot, db, SimpleNamespace(owner_ids=(999,)))
        warnings = [m for m in api.calls if m.__api_method__ == 'sendMessage']
        self.assertIn('Удалите её вручную', warnings[0].text)
        self.assertTrue(all(m.ephemeral_message_parameters.receiver_user_id == 111 for m in warnings))
        await bot.session.close()

    async def test_welcome_is_private_and_prompt_is_deleted(self):
        callback = SimpleNamespace(id='cb', from_user=TelegramUser(id=111, is_bot=False, first_name='Test'), message=SimpleNamespace(delete=AsyncMock()))
        bot = AsyncMock()
        await send_private_welcome(callback, bot, SimpleNamespace(telegram_id=-100, title='<Group>'))
        params = bot.send_message.call_args.kwargs['ephemeral_message_parameters']
        self.assertEqual(params.receiver_user_id, 111)
        self.assertEqual(params.callback_query_id, 'cb')
        callback.message.delete.assert_awaited_once()

    async def test_private_welcome_failure_never_falls_back_to_public(self):
        callback = SimpleNamespace(id='cb', from_user=TelegramUser(id=111, is_bot=False, first_name='Test'), message=SimpleNamespace(delete=AsyncMock()))
        bot = AsyncMock()
        bot.send_message.side_effect = TelegramBadRequest(method=SendMessage(chat_id=-100, text='x'), message='not supported')
        await send_private_welcome(callback, bot, SimpleNamespace(telegram_id=-100, title='Group'))
        self.assertEqual([call.args[0] for call in bot.send_message.call_args_list], [-100, 111])
        self.assertIn('ephemeral_message_parameters', bot.send_message.call_args_list[0].kwargs)

    async def test_announcement_only_replaces_own_pin(self):
        bot = AsyncMock()
        bot.id = 123
        bot.get_chat_member.return_value = SimpleNamespace(status='administrator', can_pin_messages=True)
        bot.send_message.return_value = SimpleNamespace(message_id=42)
        async with self.factory() as db:
            chat = Chat(telegram_id=-100, title='Test')
            db.add_all([chat, Setting(key='announcement_pin:-100', value='30')])
            await db.commit()
            result = await publish_announcement(bot, db, chat, 'hello')
            self.assertIn('✅', result)
            self.assertEqual((await db.get(Setting, 'announcement_pin:-100')).value, '42')
        bot.unpin_chat_message.assert_awaited_once_with(-100, message_id=30)
        bot.unpin_all_chat_messages.assert_not_called()

    async def test_pin_failure_preserves_old_pin(self):
        bot = AsyncMock()
        bot.id = 123
        bot.get_chat_member.return_value = SimpleNamespace(status='administrator', can_pin_messages=True)
        bot.send_message.return_value = SimpleNamespace(message_id=42)
        bot.pin_chat_message.side_effect = TelegramBadRequest(method=SendMessage(chat_id=-100, text='x'), message='rights')
        async with self.factory() as db:
            chat = Chat(telegram_id=-100, title='Test')
            db.add_all([chat, Setting(key='announcement_pin:-100', value='30')])
            await db.commit()
            result = await publish_announcement(bot, db, chat, 'hello')
            self.assertIn('не удалось', result)
            self.assertEqual((await db.get(Setting, 'announcement_pin:-100')).value, '30')
        bot.unpin_chat_message.assert_not_called()

    async def test_question_file_validation_and_callback_mapping(self):
        self.assertEqual(len(load_questions()), 15)
        for question in QUESTIONS:
            buttons = question_keyboard(1, question.key, question.options)
            for row in buttons.inline_keyboard:
                for button in row:
                    index = int(button.callback_data.rsplit(':', 1)[1])
                    self.assertEqual(button.text, question.options[index])
        path = Path(self.tmp.name) / 'bad.json'
        item = {'key': 'duplicate', 'text': 'Question?', 'options': ['a', 'b', 'c', 'd'], 'correct_index': 0}
        path.write_text(json.dumps([item, item]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'уникальным'):
            load_questions(path)
