from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_url: str
    log_level: str = "INFO"
    default_timezone: str = "Europe/Moscow"

    bot_mode: str = "polling"
    polling_allowed_updates: str = "message,edited_message,callback_query"

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret_token: str | None = None

    worker_batch_size: int = Field(default=100, ge=1, le=1000)
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    worker_lease_duration_seconds: int = Field(default=60, ge=1, le=86400)
    worker_send_timeout_seconds: int = Field(default=30, ge=1, le=300)
    worker_retry_base_seconds: int = Field(default=10, ge=1, le=3600)
    worker_retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    admin_ids_raw: str = Field(
        default="",
        validation_alias=AliasChoices("ADMIN_IDS", "ADMIN_IDS_RAW"),
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def allowed_updates(self) -> list[str]:
        return [item.strip() for item in self.polling_allowed_updates.split(",") if item.strip()]

    @property
    def admin_ids(self) -> set[int]:
        result: set[int] = set()
        for item in self.admin_ids_raw.split(","):
            value = item.strip()
            if value.isdigit():
                result.add(int(value))
        return result

    @property
    def normalized_bot_mode(self) -> str:
        return self.bot_mode.strip().lower()

    @property
    def webhook_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"

    @property
    def sqlalchemy_sync_database_url(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
