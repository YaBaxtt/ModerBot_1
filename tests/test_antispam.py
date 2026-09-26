import unittest
import random

from types import SimpleNamespace

from app.services.antispam import AntiSpamGuard, message_signature
from app.services.verification import generate_math_question
from app.handlers.verification import question_keyboard


class AntiSpamTests(unittest.TestCase):
    def test_four_different_messages_inside_one_second_trigger_punishment(self) -> None:
        guard = AntiSpamGuard()
        for index, moment in enumerate((0, 0.3, 0.6), start=1):
            self.assertFalse(guard.inspect(chat_id=1, user_id=2, signature=f'text:{index}', message_id=index, now=moment).delete)
        decision = guard.inspect(chat_id=1, user_id=2, signature='text:4', message_id=4, now=0.9)
        self.assertTrue(decision.delete)
        self.assertTrue(decision.punish)
        self.assertEqual(decision.message_ids, (1, 2, 3, 4))
        self.assertIn('4 сообщения', decision.reason)

    def test_four_identical_messages_inside_five_seconds_trigger_punishment(self) -> None:
        guard = AntiSpamGuard()
        for index, moment in enumerate((0, 1.6, 3.2), start=1):
            self.assertFalse(guard.inspect(chat_id=1, user_id=2, signature='text:спам', message_id=index, now=moment).punish)
        decision = guard.inspect(chat_id=1, user_id=2, signature='text:спам', message_id=4, now=4.9)
        self.assertTrue(decision.delete)
        self.assertTrue(decision.punish)
        self.assertEqual(decision.message_ids, (1, 2, 3, 4))
        self.assertIn('4 одинаковых', decision.reason)

    def test_different_content_does_not_count_as_identical(self) -> None:
        guard = AntiSpamGuard()
        for index, moment in enumerate((0, 1.5, 3, 4.5), start=1):
            decision = guard.inspect(chat_id=1, user_id=2, signature=f'sticker:{index}', now=moment)
            self.assertFalse(decision.punish)

    def test_sticker_and_gif_signatures_use_stable_file_identity(self) -> None:
        first_sticker = SimpleNamespace(text=None, caption=None, sticker=SimpleNamespace(file_unique_id='sticker-1'))
        same_sticker = SimpleNamespace(text=None, caption=None, sticker=SimpleNamespace(file_unique_id='sticker-1'))
        other_sticker = SimpleNamespace(text=None, caption=None, sticker=SimpleNamespace(file_unique_id='sticker-2'))
        gif = SimpleNamespace(text=None, caption=None, sticker=None, animation=SimpleNamespace(file_unique_id='gif-1'))
        self.assertEqual(message_signature(first_sticker), message_signature(same_sticker))
        self.assertNotEqual(message_signature(first_sticker), message_signature(other_sticker))
        self.assertEqual(message_signature(gif), 'animation:gif-1')

    def test_media_caption_does_not_hide_file_identity(self) -> None:
        first = SimpleNamespace(text=None, caption='Одинаковая подпись', animation=SimpleNamespace(file_unique_id='gif-1'))
        second = SimpleNamespace(text=None, caption='Одинаковая подпись', animation=SimpleNamespace(file_unique_id='gif-2'))
        self.assertNotEqual(message_signature(first), message_signature(second))

    def test_reset_chat_discards_partial_burst(self) -> None:
        guard = AntiSpamGuard()
        for message_id in range(1, 4):
            guard.inspect(chat_id=1, user_id=2, signature=f'text:{message_id}', message_id=message_id, now=message_id / 10)
        guard.reset_chat(1)
        decision = guard.inspect(chat_id=1, user_id=2, signature='text:4', message_id=4, now=0.4)
        self.assertFalse(decision.punish)

    def test_generated_captcha_has_one_valid_answer_up_to_fifty(self) -> None:
        for seed in range(200):
            question = generate_math_question(random.Random(seed))
            left, right = (int(value.strip(' ?')) for value in question.text.removeprefix('Сколько будет ').split('+'))
            correct = int(question.options[question.correct_index])
            self.assertEqual(correct, left + right)
            self.assertLessEqual(correct, 50)
            self.assertEqual(len(question.options), 4)
            self.assertEqual(len(set(question.options)), 4)
            self.assertTrue(all(1 <= int(value) <= 50 for value in question.options))

    def test_question_buttons_are_a_neutral_two_by_two_grid(self) -> None:
        question = generate_math_question(random.Random(1))
        keyboard = question_keyboard(1, question.key, question.options)
        self.assertEqual([len(row) for row in keyboard.inline_keyboard], [2, 2])
        self.assertTrue(all(button.style is None for row in keyboard.inline_keyboard for button in row))
