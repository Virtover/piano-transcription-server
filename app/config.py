from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "/data"
    billing_database_path: str = "/data/billing.sqlite3"
    cleanup_interval_seconds: int = Field(default=600, ge=1)
    billing_provider: Literal["none", "google_play"] = "none"
    billing_products: dict[str, int] = Field(default_factory=dict)
    google_oauth_client_id: str | None = None
    auth_jwt_secret: str | None = None
    auth_access_token_minutes: int = Field(default=15, ge=1)
    auth_refresh_token_days: int = Field(default=90, ge=1)
    free_minutes_period: str | None = None
    free_minutes: int | None = Field(default=None, ge=0)
    max_video_length_minutes: float | None = Field(default=None, gt=0)
    google_play_package_name: str | None = None
    google_play_service_account_file: str | None = None


settings = Settings()