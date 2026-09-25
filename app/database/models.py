from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# SQLite only auto-generates a rowid for a column declared exactly INTEGER
# PRIMARY KEY. PostgreSQL keeps BIGINT internal keys for headroom.
InternalId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Пользователь")
    last_name: Mapped[str | None] = mapped_column(String(255))
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chat(Base, TimestampMixin):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class UserChatStats(Base):
    __tablename__ = "user_chat_stats"
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_user_chat_stats"),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    points_earned: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemberProfileSnapshot(Base):
    __tablename__ = 'member_profile_snapshots'
    __table_args__ = (UniqueConstraint('user_id', 'chat_id', name='uq_member_profile_snapshot'),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    avatar_file_unique_id: Mapped[str | None] = mapped_column(String(255))
    avatar_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    avatar_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChatModerator(Base):
    __tablename__ = 'chat_moderators'
    __table_args__ = (UniqueConstraint('chat_id', 'user_id', name='uq_chat_moderator'),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    granted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')


class ChatPremiumAccess(Base):
    """Permanent PRO access bought for one Telegram group."""
    __tablename__ = 'chat_premium_access'
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), unique=True, nullable=False, index=True)
    purchased_by_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    price_stars: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_payment_charge_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(255))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChatProtectionSetting(Base):
    __tablename__ = 'chat_protection_settings'
    __table_args__ = (UniqueConstraint('chat_id', 'key', name='uq_chat_protection_setting'),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MemberEvent(Base):
    __tablename__ = 'member_events'
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class DailyActivity(Base):
    __tablename__ = "daily_activity"
    __table_args__ = (UniqueConstraint("user_id", "chat_id", "day", name="uq_daily_activity"),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    points_earned: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")


class DailyReward(Base, TimestampMixin):
    __tablename__ = 'daily_rewards'
    __table_args__ = (UniqueConstraint('user_id', 'day', name='uq_daily_reward_user_day'),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    reward: Mapped[int] = mapped_column(Integer, nullable=False)
    streak: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default='1')


class ActivityMessage(Base):
    __tablename__ = "activity_messages"
    __table_args__ = (UniqueConstraint("chat_id", "telegram_message_id", name="uq_activity_message"),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Warning(Base, TimestampMixin):
    __tablename__ = "warnings"
    __table_args__ = (Index("ix_warning_active", "chat_id", "target_user_id", "is_active"),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    moderator_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    removed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModerationActionType(StrEnum):
    BAN = "ban"
    UNBAN = "unban"
    MUTE = "mute"
    UNMUTE = "unmute"
    WARN = "warn"
    UNWARN = "unwarn"


class ModerationAction(Base, TimestampMixin):
    __tablename__ = "moderation_actions"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    moderator_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[ModerationActionType] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)


class ReportStatus(StrEnum):
    OPEN = "open"
    REVIEWED = "reviewed"
    CLOSED = "closed"


class Report(Base, TimestampMixin):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    reporter_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReportStatus] = mapped_column(String(16), nullable=False, default=ReportStatus.OPEN)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PrivateReport(Base, TimestampMixin):
    __tablename__ = 'private_reports'
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    reporter_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    target_username: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='open')
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdvertisingPlacement(Base):
    __tablename__ = "advertising_placements"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    link: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class RequiredSubscription(Base, TimestampMixin):
    """A Telegram channel or chat users must join before receiving access."""
    __tablename__ = 'required_subscriptions'
    __table_args__ = (
        UniqueConstraint('scope', 'owner_chat_id', 'target_telegram_id', name='uq_required_subscription_target'),
    )
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    owner_chat_id: Mapped[int | None] = mapped_column(ForeignKey('chats.id', ondelete='CASCADE'), index=True)
    target_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='true')
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))


class RequiredSubscriptionProgress(Base):
    __tablename__ = 'required_subscription_progress'
    __table_args__ = (
        UniqueConstraint('subscription_id', 'user_id', name='uq_required_subscription_progress'),
    )
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey('required_subscriptions.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdvertisingStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CLOSED = "closed"


class AdvertisingRequest(Base, TimestampMixin):
    __tablename__ = "advertising_requests"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    applicant_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    placement_id: Mapped[int | None] = mapped_column(ForeignKey("advertising_placements.id", ondelete="SET NULL"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(String(512), nullable=False)
    format: Mapped[str] = mapped_column(String(64), nullable=False)
    budget: Mapped[str] = mapped_column(String(255), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AdvertisingStatus] = mapped_column(String(16), nullable=False, default=AdvertisingStatus.NEW)


class ShopItem(Base, TimestampMixin):
    __tablename__ = "shop_items"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stock: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class PurchaseStatus(StrEnum):
    PENDING = "pending"
    FULFILLED = "fulfilled"
    REFUNDED = "refunded"
    REJECTED = "rejected"


class ShopPurchase(Base, TimestampMixin):
    __tablename__ = "shop_purchases"
    __table_args__ = (Index('uq_purchase_request_key', 'request_key', unique=True),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    buyer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("shop_items.id", ondelete="SET NULL"), nullable=True)
    price_paid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[PurchaseStatus] = mapped_column(String(16), nullable=False, default=PurchaseStatus.PENDING)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BroadcastTarget(StrEnum):
    USERS = "users"
    CHAT = "chat"
    ALL_CHATS = "all_chats"


class Broadcast(Base, TimestampMixin):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    creator_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target: Mapped[BroadcastTarget] = mapped_column(String(16), nullable=False)
    target_chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="SET NULL"))
    text: Mapped[str | None] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    button_text: Mapped[str | None] = mapped_column(String(64))
    button_url: Mapped[str | None] = mapped_column(String(512))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class CaptchaQuestion(Base, TimestampMixin):
    __tablename__ = "captcha_questions"
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class JoinVerification(Base, TimestampMixin):
    __tablename__ = "join_verifications"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_join_verification"),)
    id: Mapped[int] = mapped_column(InternalId, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    question_key: Mapped[str] = mapped_column(String(64), nullable=False)
    correct_index: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
