from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "/data"
    billing_database_path: str = "/billing/billing.sqlite3"
    billing_backup_dir: str = "/billing/backups"
    cleanup_interval_seconds: int = Field(default=600, ge=1)
    source_url_allowed_hosts: list[str] = [
        "youtube.com","youtu.be","youtube-nocookie.com",
        "vimeo.com","soundcloud.com","bandcamp.com","dailymotion.com",
        "instagram.com","instagr.am",
        "facebook.com","fb.watch",
        "tiktok.com",
        "x.com","twitter.com",
        "reddit.com","redd.it",
        "twitch.tv"
    ]
    billing_provider: Literal["none", "google_play"] = "none"
    billing_products: dict[str, int] = Field(default_factory=dict)
    google_oauth_client_ids: list[str] = Field(default_factory=list)
    free_minutes_period: str | None = None
    free_minutes: int | None = Field(default=None, ge=0)
    max_video_length_minutes: float | None = Field(default=None, gt=0)
    google_play_package_name: str | None = None
    google_play_service_account_file: str | None = None


settings = Settings()