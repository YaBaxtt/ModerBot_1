import unittest

from app.services.antispam import AntiSpamGuard
from app.services.verification import QUESTIONS, get_question
from app.handlers.verification import question_keyboard


class AntiSpamTests(unittest.TestCase):
    def test_second_message_inside_one_second_is_deleted(self) -> None:
        guard = AntiSpamGuard()
        self.assertFalse(guard.inspect(chat_id=1, user_id=2, text="one", now=0).delete)
        decision = guard.inspect(chat_id=1, user_id=2, text="two", now=0.9)
        self.assertTrue(decision.delete)
        self.assertFalse(decision.mute)

    def test_five_consecutive_repeats_trigger_mute(self) -> None:
        guard = AntiSpamGuard()
        for moment in (0, 2, 4, 6):
            self.assertFalse(guard.inspect(chat_id=1, user_id=2, text="СПАМ", now=moment).mute)
        decision = guard.inspect(chat_id=1, user_id=2, text="спам", now=8)
        self.assertTrue(decision.delete)
        self.assertTrue(decision.mute)

    def test_different_message_resets_repeat_chain(self) -> None:
        guard = AntiSpamGuard()
        for moment in (0, 2, 4, 6):
            guard.inspect(chat_id=1, user_id=2, text="same", now=moment)
        self.assertFalse(guard.inspect(chat_id=1, user_id=2, text="other", now=8).mute)

    def test_verification_question_bank_is_complete(self) -> None:
        self.assertGreaterEqual(len(QUESTIONS), 5)
        for question in QUESTIONS:
            self.assertEqual(len(question.options), 4)
            self.assertEqual(get_question(question.key), question)

    def test_question_buttons_are_a_neutral_two_by_two_grid(self) -> None:
        keyboard = question_keyboard(1, QUESTIONS[0].key, QUESTIONS[0].options)
        self.assertEqual([len(row) for row in keyboard.inline_keyboard], [2, 2])
        self.assertTrue(all(button.style is None for row in keyboard.inline_keyboard for button in row))
