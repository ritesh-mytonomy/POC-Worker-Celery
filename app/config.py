"""Settings for the API and workers (design.md §9), with startup validation (R9.5)."""
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import UUID4, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]
S3_MIN_PART_BYTES = 5 * 1024 * 1024       # S3's minimum for every part but the last


class Settings(BaseSettings):
    """Every key in design.md §9. Defaults are the POC values; `.env` and the environment override them."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Workers and Celery
    INGEST_CONCURRENCY: int = 2
    MAX_ATTEMPTS: int = 3
    TASK_SOFT_TIME_LIMIT: int = 120
    TASK_TIME_LIMIT: int = 150
    VISIBILITY_TIMEOUT: int = 300

    # Liveness and sweepers
    HEARTBEAT_SECONDS: int = 5
    STALE_AFTER_SECONDS: int = 30
    SWEEP_INTERVAL_SECONDS: int = 15
    RECONCILE_AFTER_SECONDS: int = 30
    UPLOAD_ABANDON_SECONDS: int = 3600   # staged/uploading longer → cancelled (upload-ingest-merge U4.3); prod 86400

    # Archive guards
    MAX_ZIP_ENTRIES: int = 500
    MAX_ZIP_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024
    MAX_COMPRESSION_RATIO: int = 200
    ALLOWED_ZIP_ENTRY_EXT: CsvList = ["docx"]
    ALLOWED_TOP_LEVEL_EXT: CsvList = ["docx", "zip"]
    MAX_ZIP_FOLDER_DEPTH: int = 1        # deeper entries are rejected (upload-ingest-merge U5.4)

    # POC only — absent in production
    ENTRY_DELAY_SECONDS: float = 0
    POC_ORGANIZATION_ID: UUID4 | None = None
    POC_USER_ID: int = 0                 # the system actor (OD-19) in created_by, until auth exists

    # Internal API
    INTERNAL_API_BASE_URL: str = "http://api:8000"
    INTERNAL_API_KEY: str

    # Infrastructure
    AWS_ENDPOINT_URL: str | None = None
    # Host a browser can reach, for presigned URLs (upload-ingest-merge design.md §4); unset in AWS
    S3_PUBLIC_ENDPOINT_URL: str | None = None
    S3_BUCKET: str = "clinsync-poc"
    PRESIGN_EXPIRES_SECONDS: int = 3600
    UPLOAD_PART_SIZE_BYTES: int = 8 * 1024 * 1024        # Anugrah's default; at least S3_MIN_PART_BYTES
    MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024 * 1024       # 5 GiB, Anugrah's limit
    DOWNLOAD_URL_EXPIRES_SECONDS: int = 300
    CORS_ORIGINS: CsvList = ["http://localhost:5173"]
    REDIS_URL: str = "redis://redis:6379/0"
    POSTGRES_USER: str = "clinsync"
    POSTGRES_PASSWORD: str = "clinsync"
    POSTGRES_DB: str = "clinsync"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str | None = None

    @field_validator("ALLOWED_ZIP_ENTRY_EXT", "ALLOWED_TOP_LEVEL_EXT", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Parse a comma-separated string into lowercase extensions."""
        if isinstance(value, str):
            return [part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()]
        return value

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Parse a comma-separated string of origins."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _default_database_url(self) -> "Settings":
        """Build DATABASE_URL from the POSTGRES_* values unless it is set explicitly."""
        if not self.DATABASE_URL:
            self.DATABASE_URL = (
                f"postgresql+psycopg://{quote(self.POSTGRES_USER, safe='')}:"
                f"{quote(self.POSTGRES_PASSWORD, safe='')}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        return self

    def validate(self) -> None:  # type: ignore[override]
        """Raise ValueError listing every violated §9 constraint."""
        errors: list[str] = []
        if self.INGEST_CONCURRENCY < 1:
            errors.append(f"INGEST_CONCURRENCY ({self.INGEST_CONCURRENCY}) must be >= 1")
        if self.MAX_ATTEMPTS < 1:
            errors.append(f"MAX_ATTEMPTS ({self.MAX_ATTEMPTS}) must be >= 1")
        if self.TASK_SOFT_TIME_LIMIT >= self.TASK_TIME_LIMIT:
            errors.append(
                f"TASK_SOFT_TIME_LIMIT ({self.TASK_SOFT_TIME_LIMIT}) must be < "
                f"TASK_TIME_LIMIT ({self.TASK_TIME_LIMIT})"
            )
        if self.VISIBILITY_TIMEOUT <= self.TASK_TIME_LIMIT:
            errors.append(
                f"VISIBILITY_TIMEOUT ({self.VISIBILITY_TIMEOUT}) must be > "
                f"TASK_TIME_LIMIT ({self.TASK_TIME_LIMIT})"
            )
        if self.STALE_AFTER_SECONDS < 3 * self.HEARTBEAT_SECONDS:
            errors.append(
                f"STALE_AFTER_SECONDS ({self.STALE_AFTER_SECONDS}) must be >= "
                f"3 x HEARTBEAT_SECONDS ({3 * self.HEARTBEAT_SECONDS})"
            )
        if self.UPLOAD_PART_SIZE_BYTES < S3_MIN_PART_BYTES:
            errors.append(f"UPLOAD_PART_SIZE_BYTES ({self.UPLOAD_PART_SIZE_BYTES}) must be >= 5 MiB "
                          f"({S3_MIN_PART_BYTES})")
        if not self.INTERNAL_API_KEY.strip():
            errors.append("INTERNAL_API_KEY must be non-empty")
        if errors:
            raise ValueError("Invalid configuration: " + "; ".join(errors))


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings, loaded once."""
    return Settings()
