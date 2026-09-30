from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    data_dir: str = "/data"
    billing_database_path: str = "/data/billing.sqlite3"
    cleanup_interval_seconds: int = Field(default=600, ge=1)
    billing_provider: Literal["none", "google_play"] = "none"
    billing_products: dict[str, int] = Field(default_factory=dict)
    free_minutes_period: str = "30d"
    free_minutes: int = Field(default=10, ge=0)
    max_video_length_minutes: float | None = Field(default=None, gt=0)
    google_play_package_name: str | None = None
    google_play_service_account_file: str | None = None


settings = Settings()