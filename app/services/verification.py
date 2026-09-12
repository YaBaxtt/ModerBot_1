from __future__ import annotations

from dataclasses import dataclass
from random import choice
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[str, str, str, str]
    correct_index: int


QUESTION_FILE = Path(__file__).resolve().parents[2] / 'data' / 'verification_questions.json'


def load_questions(path=QUESTION_FILE) -> tuple[Question, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(raw, list) or not raw:
            raise ValueError('ожидается непустой список вопросов')
        questions, keys = [], set()
        for item in raw:
            key, text, options, correct = item['key'], item['text'], item['options'], item['correct_index']
            if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,32}', key) or key in keys:
                raise ValueError('key должен быть уникальным, 1–32 латинских символа/цифры/_')
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise ValueError(f'{key}: неверный текст вопроса')
            if not isinstance(options, list) or len(options) != 4 or any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in options) or len(set(options)) != 4:
                raise ValueError(f'{key}: нужны четыре разных ответа до 80 символов')
            if type(correct) is not int or not 0 <= correct <= 3:
                raise ValueError(f'{key}: correct_index должен быть от 0 до 3')
            questions.append(Question(key, text, tuple(options), correct))
            keys.add(key)
        return tuple(questions)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'Ошибка в файле вопросов {path}: {exc}') from exc


QUESTIONS = load_questions()


def random_question() -> Question:
    return choice(QUESTIONS)


def get_question(key: str) -> Question | None:
    return next((question for question in QUESTIONS if question.key == key), None)
