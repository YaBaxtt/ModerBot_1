from __future__ import annotations

from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.services.parse import normalise_telegram_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(min_length=1)
    database_url: str
    owner_ids: tuple[int, ...]
    log_level: str = "INFO"
    antispam_min_interval_seconds: float = 1.0
    antispam_repeat_limit: int = 5
    antispam_repeat_window_seconds: int = 120
    antispam_mute_duration: str = "1h"
    join_verification_timeout_seconds: int = 60
    premium_price_stars: int = Field(default=50, ge=1, le=10000)
    port: int | None = Field(default=None, ge=1, le=65535)
    community_group_url: str | None = None
    community_channel_url: str | None = None

    @field_validator("owner_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value: object) -> tuple[int, ...]:
        if isinstance(value, str):
            return tuple(int(part.strip()) for part in value.split(",") if part.strip())
        if isinstance(value, (list, tuple, set)):
            return tuple(int(item) for item in value)
        return (int(value),)

    @field_validator('database_url', mode='before')
    @classmethod
    def use_async_database_driver(cls, value: object) -> str:
        """Accept Railway's standard Postgres URL and select asyncpg."""
        url = str(value).strip()
        if url.startswith('postgres://'):
            return 'postgresql+asyncpg://' + url.removeprefix('postgres://')
        if url.startswith('postgresql://'):
            return 'postgresql+asyncpg://' + url.removeprefix('postgresql://')
        return url

    @field_validator('community_group_url', 'community_channel_url', mode='before')
    @classmethod
    def parse_telegram_url(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        url = normalise_telegram_url(str(value))
        if not url:
            raise ValueError('Use a Telegram link: https://t.me/name')
        return url

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.owner_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()
