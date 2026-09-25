from __future__ import annotations

from dataclasses import dataclass
import random
import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CaptchaQuestion, Setting


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    options: tuple[str, str, str, str]
    correct_index: int


AUTO_MATH_SETTING = 'captcha_auto_math'


async def auto_math_enabled(session: AsyncSession) -> bool:
    row = await session.get(Setting, AUTO_MATH_SETTING)
    return row is None or row.value != '0'


async def set_auto_math_enabled(session: AsyncSession, enabled: bool) -> None:
    row = await session.get(Setting, AUTO_MATH_SETTING)
    if row:
        row.value = '1' if enabled else '0'
    else:
        session.add(Setting(key=AUTO_MATH_SETTING, value='1' if enabled else '0'))


async def active_custom_count(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count(CaptchaQuestion.id)).where(CaptchaQuestion.is_active.is_(True))
    )
    return int(count or 0)


def generate_math_question(rng: random.Random | None = None) -> Question:
    """Generate a four-option addition problem whose answer never exceeds 50."""
    source = rng or random.SystemRandom()
    left = source.randint(1, 40)
    right = source.randint(1, 50 - left)
    correct = left + right
    distractors: set[int] = set()
    nearby = [value for value in range(max(1, correct - 10), min(50, correct + 10) + 1) if value != correct]
    source.shuffle(nearby)
    distractors.update(nearby[:3])
    while len(distractors) < 3:
        value = source.randint(1, 50)
        if value != correct:
            distractors.add(value)
    values = [correct, *distractors]
    source.shuffle(values)
    options = tuple(str(value) for value in values)
    return Question(
        key=f'math_{secrets.token_hex(6)}',
        text=f'Сколько будет {left} + {right}?',
        options=options,  # type: ignore[arg-type]
        correct_index=values.index(correct),
    )


def custom_question(row: CaptchaQuestion) -> Question | None:
    options = row.options
    if (
        not isinstance(options, list)
        or len(options) != 4
        or any(not isinstance(value, str) or not value.strip() for value in options)
        or len({value.casefold() for value in options}) != 4
        or not 0 <= row.correct_index <= 3
    ):
        return None
    return Question(
        key=f'custom_{row.id}',
        text=row.question,
        options=tuple(options),  # type: ignore[arg-type]
        correct_index=row.correct_index,
    )


async def random_question(session: AsyncSession) -> Question:
    custom_count = await active_custom_count(session)
    automatic = await auto_math_enabled(session)
    if custom_count and (not automatic or random.SystemRandom().choice((True, False))):
        offset = random.SystemRandom().randrange(custom_count)
        row = await session.scalar(
            select(CaptchaQuestion)
            .where(CaptchaQuestion.is_active.is_(True))
            .order_by(CaptchaQuestion.id)
            .offset(offset)
            .limit(1)
        )
        if row and (question := custom_question(row)) is not None:
            return question
    # A corrupt or empty custom bank must never lock every newcomer out.
    return generate_math_question()
