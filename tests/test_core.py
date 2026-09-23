import unittest
from html import escape

from aiogram.types import EphemeralMessageParameters

from app.database.models import Base
from app.database.models import BroadcastTarget
from app.handlers.broadcasts import TARGETS
from app.keyboards.common import admin_menu, main_menu
from app.services.text import user_label, user_profile_url
from app.services.parse import is_reasonable_url, parse_duration
from app.config import Settings


class CoreTests(unittest.TestCase):
    def test_main_menu_has_new_community_controls(self):
        markup = main_menu(bot_username='moder_bot', group_url='https://t.me/group', channel_url='https://t.me/channel')
        buttons = [button for row in markup.inline_keyboard for button in row]
        labels = {button.text for button in buttons}
        self.assertTrue({'➕ Добавить меня в группу ➕', '⚙️ Настройки группы', '🛡 Модераторская', '👥 Группа', '📣 Канал', 'ℹ️ Информация'} <= labels)
        self.assertNotIn('🆘 Поддержка', labels)
        add = next(button for button in buttons if button.text == '➕ Добавить меня в группу ➕')
        self.assertEqual(add.url, 'https://t.me/moder_bot?startgroup=true')
        self.assertEqual([button.text for button in markup.inline_keyboard[2]], ['👥 Группа', '📣 Канал'])
        self.assertEqual([button.text for button in markup.inline_keyboard[3]], ['👤 Профиль', 'ℹ️ Информация'])
        self.assertEqual([button.text for button in markup.inline_keyboard[4]], ['🎁 Бонус', '🏆 Топ'])
        self.assertEqual([button.text for button in markup.inline_keyboard[5]], ['🛍 Магазин', '💼 Реклама'])
        self.assertEqual([button.text for button in markup.inline_keyboard[6]], ['📜 Правила', 'ℹ️ Помощь'])
        self.assertEqual([button.text for button in markup.inline_keyboard[8]], ['🛡 Модераторская'])

    def test_admin_menu_is_sorted_in_two_column_rows(self):
        rows = admin_menu().inline_keyboard
        self.assertEqual([button.text for button in rows[0]], ['🚨 Центр внимания', '📊 Статистика'])
        self.assertEqual([button.text for button in rows[1]], ['💬 Чаты', '🧩 Функции'])
        self.assertEqual([button.text for button in rows[2]], ['🚨 Жалобы', '🩺 Проверить бота'])
        self.assertEqual([button.text for button in rows[3]], ['📢 Рассылка', '📌 Объявление'])
        self.assertEqual([button.text for button in rows[4]], ['🛍 Добавить товар', '⚙️ Правила'])
        self.assertEqual([button.text for button in rows[5]], ['🔗 Ссылки меню', '📋 Команды'])
        self.assertEqual([button.text for button in rows[6]], ['📢 Обязательная подписка'])

    def test_owner_identity_uses_telegram_id_even_for_database_user(self):
        db_user = type('DbUser', (), {'id': 7, 'telegram_id': 123456, 'first_name': 'Name', 'username': None})()
        label = user_label(db_user)
        self.assertIn('tg://user?id=123456', label)
        self.assertIn('<code>123456</code>', label)

    def test_identity_has_clickable_username_and_id_fallback(self):
        with_username = type('TelegramUser', (), {'id': 77, 'first_name': 'Test', 'last_name': 'User', 'username': 'test_user'})()
        without_username = type('TelegramUser', (), {'id': 88, 'first_name': 'No Username', 'last_name': None, 'username': None})()
        label = user_label(with_username)
        self.assertIn('tg://user?id=77', label)
        self.assertIn('https://t.me/test_user', label)
        self.assertIn('Test User', label)
        self.assertEqual(user_profile_url(with_username), 'https://t.me/test_user')
        self.assertEqual(user_profile_url(without_username), 'tg://user?id=88')

    def test_main_actions_have_meaningful_colors(self):
        buttons = [button for row in main_menu(is_owner=True, bot_username='moder_bot').inline_keyboard for button in row]
        styles = {button.callback_data: button.style for button in buttons if button.callback_data}
        self.assertIsNone(styles['menu:report_help'])
        self.assertIsNone(styles['shop:list'])
        self.assertEqual(styles['admin:home'], 'success')
        self.assertEqual(sum(button.style == 'success' for button in buttons), 3)
        self.assertFalse(any(button.style in {'primary', 'danger'} for button in buttons))
        admin_buttons = [button for row in admin_menu().inline_keyboard for button in row]
        self.assertEqual(sum(button.style == 'success' for button in admin_buttons), 3)
        self.assertFalse(any(button.style in {'primary', 'danger'} for button in admin_buttons))

    def test_group_controls_are_hidden_without_verified_access(self):
        buttons = [
            button
            for row in main_menu(
                bot_username='moder_bot',
                has_group_settings=False,
                has_moderator_access=False,
            ).inline_keyboard
            for button in row
        ]
        callbacks = {button.callback_data for button in buttons if button.callback_data}
        self.assertNotIn('menu:group_settings', callbacks)
        self.assertNotIn('moder:home', callbacks)
        self.assertEqual(buttons[0].url, 'https://t.me/moder_bot?startgroup=true')
    def test_duration_parser(self) -> None:
        self.assertEqual(parse_duration("10m").total_seconds(), 600)
        self.assertEqual(parse_duration("7d").days, 7)
        self.assertIsNone(parse_duration("0h"))
        self.assertIsNone(parse_duration("90x"))

    def test_url_validation(self) -> None:
        self.assertTrue(is_reasonable_url("https://example.org/path"))
        self.assertTrue(is_reasonable_url("t.me/example"))
        self.assertFalse(is_reasonable_url("not a link"))

    def test_railway_postgres_url_uses_async_driver(self) -> None:
        settings = Settings(
            _env_file=None,
            bot_token='token',
            database_url='postgresql://user:pass@postgres.railway.internal:5432/railway',
            owner_ids='1,2',
        )
        self.assertEqual(settings.database_url, 'postgresql+asyncpg://user:pass@postgres.railway.internal:5432/railway')

    def test_required_tables_exist(self) -> None:
        expected = {"users", "chats", "warnings", "moderation_actions", "reports", "advertising_requests", "shop_items", "shop_purchases", "daily_activity", "broadcasts", "required_subscriptions", "required_subscription_progress"}
        self.assertTrue(expected.issubset(Base.metadata.tables))

    def test_html_escaping_for_telegram_messages(self) -> None:
        self.assertEqual(escape("< baxt & friends"), "&lt; baxt &amp; friends")

    def test_native_ephemeral_profile_parameters(self) -> None:
        self.assertEqual(EphemeralMessageParameters(receiver_user_id=123).receiver_user_id, 123)

    def test_broadcast_ui_targets_match_database_enum(self) -> None:
        self.assertEqual(TARGETS["users"], BroadcastTarget.USERS)
        self.assertEqual(TARGETS["chats"], BroadcastTarget.ALL_CHATS)


if __name__ == "__main__":
    unittest.main()
