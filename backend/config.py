"""
Centralized configuration for the Samni Labs backend.

All configuration is read from environment variables. A singleton `Settings`
instance is exposed via `get_settings()` (cached with `lru_cache`) so every
module in the app sees the same validated config object.

In the "dev" environment sensible defaults are used so a developer can boot
the app locally without any .env file. In any non-dev environment the
required AWS/Cognito/SNS identifiers MUST be set explicitly — the app will
refuse to start otherwise. This prevents accidental production boots with
development-pointing resources.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


EnvironmentName = Literal["dev", "staging", "production"]


class Settings(BaseSettings):
    """
    Strongly-typed application settings loaded from environment variables.

    Pydantic validates types and constraints at construction time; any
    invalid value (bad URL, out-of-range threshold, unknown log level)
    causes startup to fail fast with a clear error.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Environment / logging
    # ------------------------------------------------------------------ #
    ENVIRONMENT: EnvironmentName = Field(
        default="dev",
        description="Deployment environment. Controls strict validation of required fields.",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Python logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    APP_VERSION: str = Field(
        default="1.0.0",
        description="Application version string reported by /health.",
    )

    # ------------------------------------------------------------------ #
    # AWS core
    # ------------------------------------------------------------------ #
    AWS_REGION: str = Field(
        default="us-east-1",
        description="AWS region for all boto3 clients.",
    )

    # ------------------------------------------------------------------ #
    # S3
    # ------------------------------------------------------------------ #
    S3_BUCKET: str = Field(
        default="samni-phi-documents-dev",
        description="S3 bucket holding all PHI documents (PDFs, audio, transcripts, generated PDFs).",
    )
    S3_PRESIGNED_EXPIRY: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="Presigned URL TTL in seconds. Must be 60s–1h.",
    )

    # ------------------------------------------------------------------ #
    # SQS
    # ------------------------------------------------------------------ #
    SQS_QUEUE_URL: str = Field(
        default="",
        description="URL of the SQS queue used to dispatch pipeline jobs to the worker.",
    )
    SQS_WAIT_TIME_SECONDS: int = Field(
        default=20,
        ge=0,
        le=20,
        description="Long-poll wait time for SQS ReceiveMessage (max 20 per AWS).",
    )
    SQS_VISIBILITY_TIMEOUT: int = Field(
        default=900,
        ge=30,
        le=43200,
        description="SQS message visibility timeout in seconds while the worker is processing.",
    )
    SQS_MAX_MESSAGES_PER_POLL: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Max messages to fetch per SQS poll (max 10 per AWS).",
    )

    # ------------------------------------------------------------------ #
    # PostgreSQL
    # ------------------------------------------------------------------ #
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:password@localhost:5432/samni_dev",
        description="Async SQLAlchemy DSN (asyncpg driver) used by the app and worker.",
    )
    DATABASE_URL_SYNC: str = Field(
        default="postgresql://postgres:password@localhost:5432/samni_dev",
        description="Sync DSN used by Alembic migrations.",
    )
    DB_POOL_SIZE: int = Field(
        default=10,
        ge=1,
        le=100,
        description="SQLAlchemy async engine pool size.",
    )
    DB_MAX_OVERFLOW: int = Field(
        default=5,
        ge=0,
        le=100,
        description="SQLAlchemy async engine overflow connections beyond pool_size.",
    )
    DB_POOL_TIMEOUT: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Seconds to wait for a connection from the pool before erroring.",
    )
    DB_ECHO: bool = Field(
        default=False,
        description="If true, SQLAlchemy logs every SQL statement. Leave off in production (PHI risk).",
    )

    # ------------------------------------------------------------------ #
    # Cognito
    # ------------------------------------------------------------------ #
    COGNITO_USER_POOL_ID: str = Field(
        default="",
        description="Cognito User Pool ID, e.g. 'us-east-1_AbCdEfGhI'.",
    )
    COGNITO_APP_CLIENT_ID: str = Field(
        default="",
        description="Cognito App Client ID used as the JWT audience claim.",
    )
    COGNITO_JWKS_CACHE_TTL: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Seconds to cache the Cognito JWKS before refetching.",
    )

    # ------------------------------------------------------------------ #
    # SNS
    # ------------------------------------------------------------------ #
    SNS_TOPIC_ARN: str = Field(
        default="",
        description="SNS topic ARN used for caregiver push notifications (3 triggers).",
    )

    # ------------------------------------------------------------------ #
    # Bedrock (LLM)
    # ------------------------------------------------------------------ #
    BEDROCK_MODEL_ID: str = Field(
        default="anthropic.claude-sonnet-4-20250514",
        description="Bedrock model ID for all Claude invocations (field mapping + critic).",
    )
    BEDROCK_TEMPERATURE: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Temperature for Claude. Kept low for deterministic medical field extraction.",
    )
    BEDROCK_MAX_TOKENS: int = Field(
        default=4096,
        ge=256,
        le=8192,
        description="max_tokens for Claude responses.",
    )
    BEDROCK_TIMEOUT_SECONDS: int = Field(
        default=60,
        ge=5,
        le=300,
        description="Bedrock invoke timeout in seconds.",
    )

    # ------------------------------------------------------------------ #
    # Transcribe Medical
    # ------------------------------------------------------------------ #
    TRANSCRIBE_LANGUAGE_CODE: str = Field(
        default="en-US",
        description="Transcribe Medical language code.",
    )
    TRANSCRIBE_SPECIALTY: str = Field(
        default="PRIMARYCARE",
        description="Transcribe Medical specialty. PRIMARYCARE is the best fit for assisted-living dictations.",
    )
    TRANSCRIBE_TYPE: str = Field(
        default="DICTATION",
        description="Transcribe Medical job type. DICTATION = single speaker.",
    )
    TRANSCRIBE_POLL_INTERVAL_SECONDS: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Seconds between Transcribe Medical job-status polls.",
    )
    TRANSCRIBE_TIMEOUT_SECONDS: int = Field(
        default=300,
        ge=30,
        le=1800,
        description="Total timeout for a Transcribe Medical job (default 5 minutes).",
    )

    # ------------------------------------------------------------------ #
    # Pipeline behavior
    # ------------------------------------------------------------------ #
    CONFIDENCE_THRESHOLD: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Auto-approve updates at or above this confidence; anything below routes to human review.",
    )
    WORKER_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Number of pipeline jobs the worker processes concurrently.",
    )

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.upper().strip()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in valid:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(valid)}, got '{value}'"
            )
        return normalized

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_async_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the 'postgresql+asyncpg://' scheme "
                "(async SQLAlchemy + asyncpg driver)."
            )
        parsed = urlparse(value)
        if not parsed.hostname or not parsed.path.lstrip("/"):
            raise ValueError("DATABASE_URL must include a host and a database name.")
        return value

    @field_validator("DATABASE_URL_SYNC")
    @classmethod
    def _validate_sync_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql://"):
            raise ValueError(
                "DATABASE_URL_SYNC must use the 'postgresql://' scheme (used by Alembic)."
            )
        parsed = urlparse(value)
        if not parsed.hostname or not parsed.path.lstrip("/"):
            raise ValueError("DATABASE_URL_SYNC must include a host and a database name.")
        return value

    @field_validator("SQS_QUEUE_URL")
    @classmethod
    def _validate_sqs_url(cls, value: str, info: ValidationInfo) -> str:
        # Allowed to be empty in dev; non-dev strictness is enforced after
        # model construction in `_enforce_production_requirements`.
        if value and not value.startswith("https://sqs."):
            raise ValueError(
                "SQS_QUEUE_URL must be a full https://sqs.<region>.amazonaws.com/... URL."
            )
        return value

    @field_validator("SNS_TOPIC_ARN")
    @classmethod
    def _validate_sns_arn(cls, value: str) -> str:
        if value and not value.startswith("arn:aws:sns:"):
            raise ValueError("SNS_TOPIC_ARN must be a full SNS topic ARN (arn:aws:sns:...).")
        return value

    @field_validator("COGNITO_USER_POOL_ID")
    @classmethod
    def _validate_cognito_pool(cls, value: str) -> str:
        if value and "_" not in value:
            raise ValueError(
                "COGNITO_USER_POOL_ID is not in the expected '<region>_<id>' format."
            )
        return value

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.ENVIRONMENT == "production"

    @property
    def is_dev(self) -> bool:
        """True when running in the local/dev environment."""
        return self.ENVIRONMENT == "dev"

    @property
    def cognito_issuer(self) -> str:
        """The Cognito issuer URL used for JWT `iss` validation."""
        if not self.COGNITO_USER_POOL_ID:
            raise RuntimeError(
                "COGNITO_USER_POOL_ID is not configured; cannot build issuer URL."
            )
        return (
            f"https://cognito-idp.{self.AWS_REGION}.amazonaws.com/"
            f"{self.COGNITO_USER_POOL_ID}"
        )

    @property
    def cognito_jwks_url(self) -> str:
        """The Cognito JWKS endpoint for JWT signature verification."""
        return f"{self.cognito_issuer}/.well-known/jwks.json"

    # ------------------------------------------------------------------ #
    # Cross-field enforcement
    # ------------------------------------------------------------------ #
    def enforce_production_requirements(self) -> None:
        """
        Raise RuntimeError if running outside `dev` without all AWS identifiers set.

        This is called once at app/worker startup. In `dev` we tolerate empty
        AWS config so a developer can run unit tests against moto/local
        Postgres without any real AWS resources.
        """
        if self.is_dev:
            return

        required: dict[str, str] = {
            "SQS_QUEUE_URL": self.SQS_QUEUE_URL,
            "COGNITO_USER_POOL_ID": self.COGNITO_USER_POOL_ID,
            "COGNITO_APP_CLIENT_ID": self.COGNITO_APP_CLIENT_ID,
            "SNS_TOPIC_ARN": self.SNS_TOPIC_ARN,
            "S3_BUCKET": self.S3_BUCKET,
            "DATABASE_URL": self.DATABASE_URL,
            "DATABASE_URL_SYNC": self.DATABASE_URL_SYNC,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"Environment is '{self.ENVIRONMENT}' but the following required "
                f"configuration values are missing or empty: {', '.join(missing)}. "
                f"Refusing to start."
            )

        # Guardrail: default dev-bucket name must never reach non-dev.
        if self.S3_BUCKET.endswith("-dev"):
            raise RuntimeError(
                f"S3_BUCKET='{self.S3_BUCKET}' looks like a dev bucket but "
                f"ENVIRONMENT='{self.ENVIRONMENT}'. Refusing to start."
            )

        # Guardrail: default dev DSN must never reach non-dev.
        if "localhost" in self.DATABASE_URL or "localhost" in self.DATABASE_URL_SYNC:
            raise RuntimeError(
                f"DATABASE_URL points at localhost but ENVIRONMENT='{self.ENVIRONMENT}'. "
                f"Refusing to start."
            )


def configure_logging(settings: Settings) -> None:
    """
    Configure the root logger exactly once.

    PHI safety: the log format deliberately does NOT include request bodies or
    response payloads. Individual modules must log only non-PHI metadata
    (patient_id, run_id, field_path, confidence, status).
    """
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    root = logging.getLogger()

    # Avoid duplicate handlers when uvicorn reloads or when called twice.
    if getattr(root, "_samni_configured", False):
        root.setLevel(level)
        return

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s %(levelname)s [%(name)s] "
            "env=" + settings.ENVIRONMENT + " %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down noisy third-party loggers. boto3 at DEBUG dumps full request
    # bodies which can contain S3 keys and signed URLs.
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    root._samni_configured = True  # type: ignore[attr-defined]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the process-wide singleton `Settings`.

    The first call reads environment variables (and optionally a .env file),
    validates them, enforces production requirements, and configures logging.
    Subsequent calls return the cached instance.
    """
    try:
        settings = Settings()
    except Exception as exc:  # pydantic ValidationError or anything else
        # We intentionally print to stderr because logging may not be
        # configured yet at this point.
        print(
            f"FATAL: failed to load application settings: {exc}",
            flush=True,
        )
        raise

    configure_logging(settings)
    settings.enforce_production_requirements()

    logger.info(
        "Settings loaded: env=%s region=%s bucket=%s model=%s "
        "confidence_threshold=%.2f worker_concurrency=%d",
        settings.ENVIRONMENT,
        settings.AWS_REGION,
        settings.S3_BUCKET,
        settings.BEDROCK_MODEL_ID,
        settings.CONFIDENCE_THRESHOLD,
        settings.WORKER_CONCURRENCY,
    )
    return settings


def reload_settings() -> Settings:
    """
    Clear the cache and reload settings. Intended for tests only.

    Production code must never call this — callers rely on a stable singleton.
    """
    get_settings.cache_clear()
    # Clear any env-var-derived state that may have been cached elsewhere.
    os.environ.setdefault("_SAMNI_SETTINGS_RELOADED", "1")
    return get_settings()


__all__ = [
    "Settings",
    "EnvironmentName",
    "get_settings",
    "reload_settings",
    "configure_logging",
]
