from __future__ import annotations

from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatProtectionSetting, RequiredSubscription, RequiredSubscriptionProgress, Setting, User


PRIVATE_SETTING = 'required_subscription_private_enabled'
GROUP_SETTING = 'required_subscription'


async def subscription_enabled(session: AsyncSession, owner_chat_id: int | None) -> bool:
    if owner_chat_id is None:
        row = await session.get(Setting, PRIVATE_SETTING)
        return bool(row and row.value == '1')
    row = await session.scalar(select(ChatProtectionSetting).where(
        ChatProtectionSetting.chat_id == owner_chat_id,
        ChatProtectionSetting.key == GROUP_SETTING,
    ))
    return bool(row and row.enabled)


async def set_subscription_enabled(session: AsyncSession, owner_chat_id: int | None, enabled: bool) -> None:
    if owner_chat_id is None:
        row = await session.get(Setting, PRIVATE_SETTING)
        if row:
            row.value = '1' if enabled else '0'
        else:
            session.add(Setting(key=PRIVATE_SETTING, value='1' if enabled else '0'))
        return
    row = await session.scalar(select(ChatProtectionSetting).where(
        ChatProtectionSetting.chat_id == owner_chat_id,
        ChatProtectionSetting.key == GROUP_SETTING,
    ))
    if row:
        row.enabled = enabled
    else:
        session.add(ChatProtectionSetting(chat_id=owner_chat_id, key=GROUP_SETTING, enabled=enabled))


async def subscription_links(session: AsyncSession, owner_chat_id: int | None) -> list[RequiredSubscription]:
    scope = 'private' if owner_chat_id is None else 'group'
    query = select(RequiredSubscription).where(
        RequiredSubscription.scope == scope,
        RequiredSubscription.is_active.is_(True),
    )
    query = query.where(RequiredSubscription.owner_chat_id.is_(None)) if owner_chat_id is None else query.where(RequiredSubscription.owner_chat_id == owner_chat_id)
    return list((await session.scalars(query.order_by(RequiredSubscription.id))).all())


def member_is_subscribed(member) -> bool:
    if member.status == 'restricted':
        return bool(getattr(member, 'is_member', False))
    return member.status in {'member', 'administrator', 'creator'}


async def missing_subscriptions(bot, links: list[RequiredSubscription], telegram_user_id: int) -> list[RequiredSubscription]:
    missing = []
    for link in links:
        try:
            member = await bot.get_chat_member(link.target_telegram_id, telegram_user_id)
        except TelegramAPIError:
            missing.append(link)
            continue
        if not member_is_subscribed(member):
            missing.append(link)
    return missing


async def _progress(session: AsyncSession, subscription_id: int, user_id: int) -> RequiredSubscriptionProgress:
    row = await session.scalar(select(RequiredSubscriptionProgress).where(
        RequiredSubscriptionProgress.subscription_id == subscription_id,
        RequiredSubscriptionProgress.user_id == user_id,
    ))
    if not row:
        row = RequiredSubscriptionProgress(subscription_id=subscription_id, user_id=user_id)
        session.add(row)
        await session.flush()
    return row


async def record_impressions(session: AsyncSession, links: list[RequiredSubscription], user: User) -> None:
    now = datetime.now(timezone.utc)
    for link in links:
        row = await _progress(session, link.id, user.id)
        row.impressions += 1
        row.last_seen_at = now


async def record_check(session: AsyncSession, links: list[RequiredSubscription], missing: list[RequiredSubscription], user: User) -> None:
    now = datetime.now(timezone.utc)
    missing_ids = {link.id for link in missing}
    for link in links:
        row = await _progress(session, link.id, user.id)
        row.checks += 1
        row.last_checked_at = now
        if link.id not in missing_ids and row.passed_at is None:
            row.passed_at = now


async def subscription_stats(session: AsyncSession, subscription_id: int) -> dict[str, int]:
    row = (await session.execute(select(
        func.coalesce(func.sum(RequiredSubscriptionProgress.impressions), 0),
        func.count(RequiredSubscriptionProgress.id),
        func.coalesce(func.sum(RequiredSubscriptionProgress.checks), 0),
        func.count(RequiredSubscriptionProgress.passed_at),
    ).where(RequiredSubscriptionProgress.subscription_id == subscription_id))).one()
    return {'impressions': int(row[0]), 'users': int(row[1]), 'checks': int(row[2]), 'passed': int(row[3])}
