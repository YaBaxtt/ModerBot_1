"""Persistent global feature switches controlled by bot owners."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Setting


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    description: str


FEATURES = (
    Feature('verification', 'Проверка новичков', 'Мут и тест при входе'),
    Feature('antispam', 'Авто-антиспам', 'Удаление флуда и автоматический мут'),
    Feature('moderation', 'Команды модерации', 'Ban, mute, warn и история'),
    Feature('command_cleanup', 'Очистка /команд', 'Удаление команд пользователей из групп'),
    Feature('member_tracking', 'Изменения профиля', 'Имя, username и аватар участников'),
    Feature('points', 'Очки активности', 'Начисление очков за сообщения'),
    Feature('leaderboard', 'Рейтинг / топ', 'Таблица лидеров сообщества'),
    Feature('daily_reward', 'Ежедневная награда', 'Бонус очков и серия дней'),
    Feature('shop', 'Магазин', 'Просмотр и покупка товаров'),
    Feature('reports', 'Жалобы', 'Личные и групповые обращения'),
    Feature('advertising', 'Рекламные заявки', 'Форма сотрудничества'),
    Feature('broadcasts', 'Рассылки', 'Создание новых массовых рассылок'),
    Feature('announcements', 'Объявления', 'Публикация и закрепление в чатах'),
)
FEATURE_BY_KEY = {feature.key: feature for feature in FEATURES}


async def feature_states(session: AsyncSession, keys=None) -> dict[str, bool]:
    selected = tuple(keys or FEATURE_BY_KEY)
    rows = (await session.execute(select(Setting.key, Setting.value).where(Setting.key.in_([f'feature:{key}' for key in selected])))).all()
    saved = {key.removeprefix('feature:'): value for key, value in rows}
    return {key: saved.get(key, '1') not in {'0', 'false', 'off'} for key in selected}


async def feature_enabled(session: AsyncSession, key: str) -> bool:
    return (await feature_states(session, (key,)))[key]


async def set_feature(session: AsyncSession, key: str, enabled: bool) -> None:
    if key not in FEATURE_BY_KEY:
        raise KeyError(key)
    setting_key = f'feature:{key}'
    row = await session.get(Setting, setting_key)
    if row:
        row.value = '1' if enabled else '0'
    else:
        session.add(Setting(key=setting_key, value='1' if enabled else '0'))
