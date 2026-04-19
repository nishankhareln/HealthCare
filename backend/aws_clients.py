"""
Centralized boto3 client factory for the Samni Labs backend.

All AWS clients are created via `lru_cache` so the process holds exactly one
shared client per service. Boto3 low-level clients are documented as
thread-safe for individual operations, so sharing them between the FastAPI
event loop and the worker's concurrent asyncio tasks is safe.

Database operations use SQLAlchemy + asyncpg (see database.py) — there is
no DynamoDB client here because the project deliberately does not use DynamoDB.

Security posture:
  * TLS is enforced on every client (use_ssl=True, explicit).
  * Signature v4 everywhere; `s3v4` for S3 so presigned URLs validate in every
    region (us-east-1 has a legacy quirk) and are compatible with SSE-KMS.
  * Connect + read timeouts on every client to prevent slow-loris / hung-socket
    resource exhaustion.
  * Adaptive retry mode respects AWS throttling backoff instead of hammering.
  * A User-Agent extra stamp ties every AWS API call back to this service so
    CloudTrail queries can isolate our traffic.
  * No credentials, no raw request/response bodies, no S3 keys, and no signed
    URLs are ever logged at INFO or above.

Credential resolution follows the standard boto3 provider chain: environment
variables -> shared credentials file -> container / instance role. The app
code itself NEVER reads AWS_ACCESS_KEY_ID — credentials live outside the
process image.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Callable

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config import Settings, get_settings


logger = logging.getLogger(__name__)


# Stamped onto every AWS call's User-Agent. CloudTrail keeps the full UA, so
# this lets us attribute calls back to the service even in a shared account.
_USER_AGENT_EXTRA = "samni-backend/1.0.0"


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _base_config(settings: Settings, **overrides: Any) -> Config:
    """
    Build a `botocore.config.Config` with secure defaults.

    Any keyword passed in `overrides` replaces the matching default so each
    client can tune its own timeouts/retries without losing the hardened base.
    """
    defaults: dict[str, Any] = {
        "region_name": settings.AWS_REGION,
        "signature_version": "v4",
        "retries": {"max_attempts": 5, "mode": "adaptive"},
        "connect_timeout": 10,
        "read_timeout": 30,
        "user_agent_extra": _USER_AGENT_EXTRA,
        # Bound concurrent HTTP connections per client so a burst of work
        # cannot exhaust the host's file descriptors.
        "max_pool_connections": 20,
        # Some APIs (e.g. Bedrock) return structured error bodies; make sure
        # we always parse them rather than treating errors as opaque strings.
        "parameter_validation": True,
    }
    defaults.update(overrides)
    return Config(**defaults)


@lru_cache(maxsize=1)
def _boto_session() -> boto3.session.Session:
    """
    Process-wide boto3 Session.

    Region comes from Settings; credentials are resolved by the standard
    boto3 provider chain (env vars, shared credentials file, IAM role).
    """
    settings = get_settings()
    return boto3.session.Session(region_name=settings.AWS_REGION)


def _log_init(service: str, **fields: Any) -> None:
    """Uniform, PHI-safe init log. Never logs ARNs, keys, or URLs in full."""
    safe_fields = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("Initialized AWS client service=%s %s", service, safe_fields)


# --------------------------------------------------------------------------- #
# Per-service client factories (all singletons)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_s3_client() -> BaseClient:
    """
    S3 client configured for presigned URL generation.

    `signature_version='s3v4'` is required for presigned URLs to validate in
    every region and to be compatible with SSE-KMS-protected buckets.
    Virtual-hosted-style addressing is explicit so URLs always take the form
    `https://<bucket>.s3.<region>.amazonaws.com/<key>`.
    """
    settings = get_settings()
    cfg = _base_config(
        settings,
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    client = _boto_session().client("s3", config=cfg, use_ssl=True)
    _log_init("s3", region=settings.AWS_REGION)
    return client


@lru_cache(maxsize=1)
def get_sqs_client() -> BaseClient:
    """
    SQS client.

    `read_timeout` MUST exceed `SQS_WAIT_TIME_SECONDS` or long-poll
    ReceiveMessage calls will be cut short by the HTTP layer before SQS
    itself returns.
    """
    settings = get_settings()
    cfg = _base_config(
        settings,
        read_timeout=max(30, settings.SQS_WAIT_TIME_SECONDS + 10),
    )
    client = _boto_session().client("sqs", config=cfg, use_ssl=True)
    _log_init("sqs", region=settings.AWS_REGION)
    return client


@lru_cache(maxsize=1)
def get_transcribe_client() -> BaseClient:
    """
    Amazon Transcribe Medical client.

    Note: the boto3 service name is "transcribe" — the "Medical" variants
    are API methods (`start_medical_transcription_job`, etc.) on that
    same client, not a separate service.
    """
    settings = get_settings()
    cfg = _base_config(settings)
    client = _boto_session().client("transcribe", config=cfg, use_ssl=True)
    _log_init(
        "transcribe",
        region=settings.AWS_REGION,
        specialty=settings.TRANSCRIBE_SPECIALTY,
        job_type=settings.TRANSCRIBE_TYPE,
    )
    return client


@lru_cache(maxsize=1)
def get_bedrock_runtime_client() -> BaseClient:
    """
    Bedrock-runtime client for Claude invocations.

    A longer read_timeout is allowed because LLM inference on a 4k-token
    prompt can legitimately take 30–60s. Retries are capped lower than the
    other clients because a failed invoke is expensive and a stuck request
    should surface to the caller quickly rather than quietly retrying.
    """
    settings = get_settings()
    cfg = _base_config(
        settings,
        read_timeout=settings.BEDROCK_TIMEOUT_SECONDS,
        retries={"max_attempts": 3, "mode": "adaptive"},
    )
    client = _boto_session().client("bedrock-runtime", config=cfg, use_ssl=True)
    _log_init(
        "bedrock-runtime",
        region=settings.AWS_REGION,
        model=settings.BEDROCK_MODEL_ID,
    )
    return client


@lru_cache(maxsize=1)
def get_sns_client() -> BaseClient:
    """SNS client for caregiver push notifications."""
    settings = get_settings()
    cfg = _base_config(settings)
    client = _boto_session().client("sns", config=cfg, use_ssl=True)
    _log_init("sns", region=settings.AWS_REGION)
    return client


@lru_cache(maxsize=1)
def get_cognito_client() -> BaseClient:
    """
    Cognito Identity Provider client.

    Day-to-day JWT validation uses the JWKS endpoint over plain HTTPS (see
    auth.py) rather than this client. The client is still exposed for admin
    operations that may be needed later (e.g. disabling compromised users).
    """
    settings = get_settings()
    cfg = _base_config(settings)
    client = _boto_session().client("cognito-idp", config=cfg, use_ssl=True)
    _log_init("cognito-idp", region=settings.AWS_REGION)
    return client


# --------------------------------------------------------------------------- #
# Connectivity probe used by startup + /health
# --------------------------------------------------------------------------- #
def _client_error_code(exc: BaseException) -> str:
    """Extract a short error code from a botocore exception without leaking PHI."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return "Unknown"


def verify_aws_connectivity() -> dict[str, bool]:
    """
    Lightweight connectivity probe.

    Used by /health and at worker startup. Chooses cheap, narrowly-scoped API
    calls so a misconfigured IAM role surfaces here rather than during a real
    pipeline run. This function NEVER raises — callers inspect the dict.
    """
    settings = get_settings()
    results: dict[str, bool] = {}

    probes: list[tuple[str, Callable[[], Any]]] = []

    # S3: head_bucket requires only s3:ListBucket on the target bucket.
    probes.append(
        (
            "s3",
            lambda: get_s3_client().head_bucket(Bucket=settings.S3_BUCKET),
        )
    )

    # SQS: skip if no queue configured (dev mode). get_queue_attributes with
    # just QueueArn is cheap and scoped.
    if settings.SQS_QUEUE_URL:
        probes.append(
            (
                "sqs",
                lambda: get_sqs_client().get_queue_attributes(
                    QueueUrl=settings.SQS_QUEUE_URL,
                    AttributeNames=["QueueArn"],
                ),
            )
        )

    # SNS: get_topic_attributes is cheap and scoped to a single topic.
    if settings.SNS_TOPIC_ARN:
        probes.append(
            (
                "sns",
                lambda: get_sns_client().get_topic_attributes(
                    TopicArn=settings.SNS_TOPIC_ARN,
                ),
            )
        )

    for name, probe in probes:
        try:
            probe()
            results[name] = True
        except (BotoCoreError, ClientError) as exc:
            code = _client_error_code(exc)
            # Only the error code is logged — codes like 'NoSuchBucket' or
            # 'AccessDenied' aid debugging without leaking bucket/topic names
            # beyond what startup already logged.
            logger.warning(
                "AWS connectivity probe failed service=%s code=%s",
                name,
                code,
            )
            results[name] = False
        except Exception as exc:  # noqa: BLE001 — defensive, never raise
            logger.warning(
                "AWS connectivity probe failed service=%s unexpected_error=%s",
                name,
                type(exc).__name__,
            )
            results[name] = False

    return results


# --------------------------------------------------------------------------- #
# Test-only helpers
# --------------------------------------------------------------------------- #
def reset_clients() -> None:
    """
    Clear all cached clients.

    Tests call this between fixtures when switching moto mock contexts.
    Not intended for production use.
    """
    _boto_session.cache_clear()
    get_s3_client.cache_clear()
    get_sqs_client.cache_clear()
    get_transcribe_client.cache_clear()
    get_bedrock_runtime_client.cache_clear()
    get_sns_client.cache_clear()
    get_cognito_client.cache_clear()
    logger.debug("Cleared all cached AWS clients")


__all__ = [
    "get_s3_client",
    "get_sqs_client",
    "get_transcribe_client",
    "get_bedrock_runtime_client",
    "get_sns_client",
    "get_cognito_client",
    "verify_aws_connectivity",
    "reset_clients",
]
