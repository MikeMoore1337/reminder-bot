import re
from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.persistent_policy import PersistentPolicy, parse_clock

_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


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
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    worker_batch_size: int = Field(default=100, ge=1, le=1000)
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=3600)
    worker_lease_duration_seconds: int = Field(default=60, ge=1, le=86400)
    worker_send_timeout_seconds: int = Field(default=30, ge=1, le=300)
    worker_lease_safety_margin_seconds: int = Field(default=10, ge=1, le=300)
    worker_retry_base_seconds: int = Field(default=10, ge=1, le=3600)
    worker_retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)

    persistent_repeat_interval_minutes: int = Field(default=60, ge=5, le=1440)
    persistent_max_deliveries: int = Field(default=6, ge=1, le=100)
    persistent_max_escalations: int = Field(default=5, ge=0, le=99)
    persistent_quiet_hours_start: str = "22:00"
    persistent_quiet_hours_end: str = "08:00"
    persistent_user_cooldown_minutes: int = Field(default=1, ge=0, le=60)

    suggestion_snooze_threshold: int = Field(default=3, ge=2, le=10)
    suggestion_window_days: int = Field(default=30, ge=7, le=90)
    suggestion_target_tolerance_minutes: int = Field(default=20, ge=1, le=120)
    suggestion_min_schedule_shift_minutes: int = Field(default=30, ge=1, le=720)
    digest_worker_enabled: bool = True
    digest_morning_time: str = "09:00"
    digest_evening_time: str = "20:00"
    digest_quiet_hours_start: str = "22:00"
    digest_quiet_hours_end: str = "08:00"
    digest_max_items: int = Field(default=20, ge=1, le=100)
    digest_max_delay_minutes: int = Field(default=360, ge=0, le=1440)
    # Keep the digest lease aligned with the existing worker lease when no
    # digest-specific override is configured. This preserves valid pre-digest
    # deployments whose send timeout and safety margin already require a
    # worker lease longer than the old fixed 60-second digest default.
    digest_lease_duration_seconds: int | None = Field(default=None, ge=1, le=86400)

    # Conditions are deliberately opt-in.  The condition poller is an
    # extension point and must never change the existing time-reminder worker
    # when it is not explicitly enabled.
    condition_worker_enabled: bool = False
    condition_poll_batch_size: int = Field(default=10, ge=1, le=100)
    condition_poll_interval_seconds: int = Field(default=300, ge=30, le=86400)
    condition_drain_max_batches: int = Field(default=10, ge=1, le=100)
    condition_drain_interval_seconds: int = Field(default=1, ge=1, le=60)
    condition_request_timeout_seconds: int = Field(default=10, ge=1, le=60)
    condition_max_response_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    condition_retry_base_seconds: int = Field(default=60, ge=1, le=86_400)
    condition_retry_max_seconds: int = Field(default=3600, ge=1, le=86_400)
    condition_lease_duration_seconds: int = Field(default=90, ge=2, le=86_400)
    condition_cleanup_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    condition_history_retention_days: int = Field(default=90, ge=1, le=3650)
    condition_authorization_env_allowlist_raw: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CONDITION_AUTHORIZATION_ENV_ALLOWLIST",
            "CONDITION_AUTHORIZATION_ENV_ALLOWLIST_RAW",
        ),
    )
    condition_authorization_env_bindings_raw: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CONDITION_AUTHORIZATION_ENV_BINDINGS",
            "CONDITION_AUTHORIZATION_ENV_BINDINGS_RAW",
        ),
    )

    voice_stt_command: str = "whisper-cli"
    voice_stt_model_path: str | None = None
    voice_stt_language: str = "ru"
    voice_stt_threads: int = Field(default=2, ge=1, le=8)
    voice_stt_timeout_seconds: int = Field(default=90, ge=1, le=600)
    voice_stt_max_concurrent_jobs: int = Field(default=1, ge=1, le=2)
    voice_stt_queue_timeout_seconds: int = Field(default=5, ge=1, le=60)
    voice_conversion_command: str = "ffmpeg"
    voice_conversion_timeout_seconds: int = Field(default=30, ge=1, le=300)
    voice_download_timeout_seconds: int = Field(default=30, ge=1, le=300)
    voice_temp_dir: str | None = None
    voice_max_file_size_bytes: int = Field(default=10_000_000, ge=1, le=20_000_000)
    voice_max_duration_seconds: int = Field(default=120, ge=1, le=120)
    voice_draft_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    voice_draft_cleanup_interval_seconds: int = Field(default=60, ge=10, le=86_400)
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

    @model_validator(mode="after")
    def validate_worker_timing(self) -> "Settings":
        if self.worker_lease_duration_seconds <= (
            self.worker_send_timeout_seconds + self.worker_lease_safety_margin_seconds
        ):
            raise ValueError(
                "worker_lease_duration_seconds must be greater than "
                "worker_send_timeout_seconds plus worker_lease_safety_margin_seconds"
            )
        if self.worker_retry_max_seconds < self.worker_retry_base_seconds:
            raise ValueError(
                "worker_retry_max_seconds must be greater than or equal to "
                "worker_retry_base_seconds"
            )
        if self.digest_lease_duration_seconds is None:
            self.digest_lease_duration_seconds = self.worker_lease_duration_seconds
        if self.digest_lease_duration_seconds <= (
            self.worker_send_timeout_seconds + self.worker_lease_safety_margin_seconds
        ):
            raise ValueError(
                "digest_lease_duration_seconds must be greater than "
                "worker_send_timeout_seconds plus worker_lease_safety_margin_seconds"
            )
        if self.condition_retry_max_seconds < self.condition_retry_base_seconds:
            raise ValueError(
                "condition_retry_max_seconds must be greater than or equal to "
                "condition_retry_base_seconds"
            )
        if self.condition_lease_duration_seconds <= (
            self.condition_request_timeout_seconds + self.worker_lease_safety_margin_seconds
        ):
            raise ValueError(
                "condition_lease_duration_seconds must be greater than "
                "condition_request_timeout_seconds plus "
                "worker_lease_safety_margin_seconds"
            )
        for raw_name in self.condition_authorization_env_allowlist_raw.split(","):
            name = raw_name.strip().upper()
            if name and not _ENV_NAME_PATTERN.fullmatch(name):
                raise ValueError(
                    "condition_authorization_env_allowlist contains an invalid environment name"
                )
        from app.services.condition_provider import ConditionProviderError

        try:
            _ = self.condition_authorization_env_bindings
        except ConditionProviderError as exc:
            raise ValueError("condition_authorization_env_bindings is invalid") from exc
        PersistentPolicy(
            interval_minutes=self.persistent_repeat_interval_minutes,
            max_deliveries=self.persistent_max_deliveries,
            max_escalations=self.persistent_max_escalations,
            quiet_hours_start=self.persistent_quiet_hours_start,
            quiet_hours_end=self.persistent_quiet_hours_end,
        )
        parse_clock(self.digest_morning_time, field_name="digest_morning_time")
        parse_clock(self.digest_evening_time, field_name="digest_evening_time")
        parse_clock(self.digest_quiet_hours_start, field_name="digest_quiet_hours_start")
        parse_clock(self.digest_quiet_hours_end, field_name="digest_quiet_hours_end")
        return self

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
    def condition_authorization_env_allowlist(self) -> frozenset[str]:
        return frozenset(
            name.strip().upper()
            for name in self.condition_authorization_env_allowlist_raw.split(",")
            if name.strip()
        )

    @property
    def condition_authorization_env_bindings(self) -> dict[str, str]:
        from app.services.condition_provider import normalize_authorization_bindings

        return normalize_authorization_bindings(
            self.condition_authorization_env_bindings_raw,
            authorization_env_allowlist=self.condition_authorization_env_allowlist,
        )

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
