"""
FastAPI application entry point for the Samni Labs backend.

Endpoints (all JSON; all except /health require a Cognito Bearer JWT):

    GET  /health                         — liveness + DB/AWS probe
    POST /get-upload-url                 — presigned S3 PUT URL
    POST /intake                         — start an intake pipeline (PDF → master)
    POST /reassessment                   — start a reassessment pipeline (audio → diff)
    GET  /review/{patient_id}            — fetch the pending caregiver review, if any
    POST /review/{patient_id}            — submit caregiver decisions, resume the paused graph
    GET  /patient/{patient_id}/status    — latest pipeline run state for the patient
    GET  /get-download-url/{patient_id}  — presigned S3 GET URL for the latest PDF

Architectural posture:
  * /intake and /reassessment are PRODUCERS. They insert a `pipeline_runs`
    row in status='queued' and enqueue an SQS message. The worker (worker.py)
    is the CONSUMER that drives the LangGraph pipeline.
  * /review is special: caregiver submissions RESUME the paused graph from
    the shared PostgreSQL checkpointer, running the remaining nodes inline
    on the API event loop. The API and worker BOTH initialise the pipeline
    singleton at startup so resume can happen from either side. Idempotency
    is enforced by a CAS on the pipeline_runs.status column.

Security posture:
  * Every request carries a server-generated `trace_id` (UUIDv4). Logs and
    error responses surface it; it is the ONLY way to correlate a failure
    across the API, the worker, and AWS — without it we would need to log
    request bodies, which would leak PHI.
  * Exception handlers collapse ALL unhandled errors to a uniform
    ErrorResponse body. Stack traces, SQLAlchemy diagnostics, and botocore
    error messages are logged server-side only — never returned to the
    caller.
  * A secure-headers middleware stamps every response with
    X-Content-Type-Options, Referrer-Policy, X-Frame-Options, and (in
    non-dev) HSTS. The API returns JSON only, but HSTS + frame-deny are
    cheap defence-in-depth against a future HTML surface being added.
  * CORS is OPEN for localhost development origins ONLY when
    ENVIRONMENT='dev'. In staging/production, CORS is disabled (mobile
    clients do not need CORS; a future web surface must opt-in via an
    explicit origin list added to config).
  * Every protected endpoint depends on `get_current_user` (auth.py), which
    verifies a Cognito JWT with RS256 pinning. An authenticated caller
    additionally passes through `_authorize_patient_access`, which refuses
    cross-facility reads of a patient record. A valid JWT is NOT enough —
    the caller's facility_id claim must match the patient's facility_id,
    or the request is 403'd.
  * Presigned S3 URLs are generated with SigV4 + SSE-KMS required on PUT.
    Keys are SERVER-constructed from the authenticated user's context;
    clients never choose the key. The client-presented `s3_key` in /intake
    and /reassessment is re-validated against S3_KEY_ALLOWED_PREFIXES and
    against the expected prefix for the pipeline type.
  * SQS send and S3 presign calls are wrapped with asyncio.to_thread so
    blocking boto3 network work never stalls the event loop.
  * No endpoint logs request/response bodies. Logs carry identifiers, HTTP
    method, path, status, duration_ms, and trace_id.

Request lifecycle (trace_id, timing, logging) is handled by a single
middleware so every code path — including handlers that raise — emits
exactly one structured line per request.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Final, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import and_, desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from auth import AuthenticatedUser, close_http_client, get_current_user
from aws_clients import (
    _client_error_code,
    get_s3_client,
    get_sqs_client,
    verify_aws_connectivity,
)
from config import Settings, get_settings
from database import check_db_connectivity, get_db, init_db, shutdown_db
from db_models import (
    PIPELINE_STATUS_COMPLETE,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_WAITING_REVIEW,
    PIPELINE_TYPE_INTAKE,
    PIPELINE_TYPE_REASSESSMENT,
    Patient,
    PipelineRun,
)
from models import (
    DownloadUrlResponse,
    ErrorResponse,
    HealthResponse,
    IntakeRequest,
    PatientStatusResponse,
    PipelineResponse,
    ReassessmentRequest,
    ReviewResponse,
    ReviewSubmission,
    SQSMessage,
    UploadUrlRequest,
    UploadUrlResponse,
)
from pipeline import (
    init_pipeline,
    shutdown_pipeline,
)
from utils import generate_run_id, get_nested, utc_now


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# Per-file-type upload extension. The upload endpoint does not accept a
# client-chosen extension because the set of allowed audio formats is tied
# to Transcribe Medical's accepted media formats (see nodes/transcribe.py).
# m4a is what the Flutter client produces on iOS by default; the worker's
# transcribe node also accepts mp3/mp4/wav/flac/ogg/amr/webm — if we need
# to broaden this, add an enum to UploadUrlRequest rather than letting the
# client pass an arbitrary extension.
_UPLOAD_EXTENSION_BY_TYPE: Final[dict[str, str]] = {
    "pdf": "pdf",
    "audio": "m4a",
}
_CONTENT_TYPE_BY_EXT: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "m4a": "audio/mp4",
}
_KEY_PREFIX_BY_TYPE: Final[dict[str, str]] = {
    "pdf": "uploads/",
    "audio": "audio/",
}

# TTL for presigned upload URLs. Shorter than S3_PRESIGNED_EXPIRY floor
# because uploads are initiated interactively — if the user sits on the URL
# for more than a few minutes we want them to re-request it.
_UPLOAD_URL_TTL_SECONDS: Final[int] = 600

# Max SQS message body size. SQS itself caps at 256 KiB; the worker's own
# parse-time cap is 128 KiB. We mirror the worker cap here so anything the
# API sends is guaranteed acceptable on the consumer side. Fresh-run
# messages are ~500 bytes; resume messages carry the caregiver's decisions
# and can be larger, which is why this cap sits well above the fresh-run
# worst case.
_MAX_SQS_BODY_BYTES: Final[int] = 128 * 1024

# Dev-only CORS allow-list. In staging/production CORS is disabled.
_DEV_CORS_ORIGINS: Final[tuple[str, ...]] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)


# --------------------------------------------------------------------------- #
# Lifespan: initialise DB + pipeline + AWS probes at startup, tear down cleanly
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Startup / shutdown hooks for the FastAPI app.

    Startup order matters:
      1. Logging + settings (get_settings() calls configure_logging()).
      2. Database engine — init_pipeline depends on Postgres being reachable
         because the LangGraph checkpointer runs migrations on open.
      3. Pipeline (LangGraph graphs + checkpointer pool).
      4. AWS connectivity probe — non-fatal (logged); the /health endpoint
         will also report this. We do not refuse to boot on a transient AWS
         miss because a degraded dependency should not take the API down.

    Shutdown is the reverse: pipeline → DB → auth HTTP client.
    """
    settings = get_settings()

    try:
        await init_db()
    except Exception:
        logger.exception("startup: init_db failed — refusing to boot")
        raise

    try:
        await init_pipeline()
    except Exception:
        logger.exception("startup: init_pipeline failed — refusing to boot")
        # Leave the DB engine intact for a clean shutdown.
        await shutdown_db()
        raise

    # AWS probes — best effort. Log misses; do not raise. In dev, any of the
    # AWS clients may be unconfigured and we still want the app to boot.
    try:
        probe = await asyncio.to_thread(verify_aws_connectivity)
        logger.info("startup: aws_probe=%s env=%s", probe, settings.ENVIRONMENT)
    except Exception:
        logger.warning("startup: aws connectivity probe raised", exc_info=False)

    logger.info(
        "startup: ready env=%s version=%s",
        settings.ENVIRONMENT,
        settings.APP_VERSION,
    )

    try:
        yield
    finally:
        # Shutdown — reverse order, each step isolated so one failure does
        # not skip the rest.
        for name, coro in (
            ("pipeline", shutdown_pipeline()),
            ("database", shutdown_db()),
            ("auth_http_client", close_http_client()),
        ):
            try:
                await coro
            except Exception:
                logger.exception("shutdown: %s teardown failed", name)
        logger.info("shutdown: complete")


# --------------------------------------------------------------------------- #
# App instance
# --------------------------------------------------------------------------- #
_settings_at_boot = get_settings()

app = FastAPI(
    title="Samni Labs Backend",
    description="Caregiver assessment pipeline API",
    version=_settings_at_boot.APP_VERSION,
    lifespan=lifespan,
    # OpenAPI docs: enabled in dev only. In production we do not publish the
    # schema — a reduced surface is a smaller phishing target.
    docs_url="/docs" if _settings_at_boot.is_dev else None,
    redoc_url="/redoc" if _settings_at_boot.is_dev else None,
    openapi_url="/openapi.json" if _settings_at_boot.is_dev else None,
)


# --------------------------------------------------------------------------- #
# Middleware (registered bottom-up: last added runs first)
# --------------------------------------------------------------------------- #
# CORS — dev-only. Mobile clients do not need CORS; a future web surface must
# be added to config and enabled explicitly.
if _settings_at_boot.is_dev:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_DEV_CORS_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


class _SecureHeadersMiddleware(BaseHTTPMiddleware):
    """
    Stamp security-relevant response headers.

    These are cheap, universally applicable, and compatible with a JSON API.
    HSTS is stamped only in non-dev because a dev client usually speaks
    plain HTTP to localhost and we do not want to poison their browser HSTS
    cache for a year.
    """

    def __init__(self, app: FastAPI, *, is_dev: bool) -> None:
        super().__init__(app)
        self._is_dev = is_dev

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if not self._is_dev:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class _TraceMiddleware(BaseHTTPMiddleware):
    """
    Attach a trace_id to every request and emit one structured log line per
    request (method, path, status, duration_ms, trace_id, client_ip).

    PHI safety: the log line NEVER includes the request body, the response
    body, or query-string values. Client IP is included so abuse patterns
    can be investigated without a body dump; if a class of client IPs is
    later considered sensitive, drop this field.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        trace_id = uuid.uuid4().hex
        request.state.trace_id = trace_id
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            try:
                client_ip = request.client.host if request.client else "-"
            except Exception:  # noqa: BLE001 — defensive
                client_ip = "-"
            logger.info(
                "http method=%s path=%s status=%d duration_ms=%d trace_id=%s ip=%s",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                trace_id,
                client_ip,
            )


app.add_middleware(_SecureHeadersMiddleware, is_dev=_settings_at_boot.is_dev)
app.add_middleware(_TraceMiddleware)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _trace_id(request: Request) -> Optional[str]:
    """Return the trace id attached by the middleware, or None in test edge cases."""
    return getattr(request.state, "trace_id", None)


def _error_response(
    *,
    status_code: int,
    error: str,
    detail: Optional[str],
    trace_id: Optional[str],
) -> JSONResponse:
    """Build a uniform ErrorResponse JSON body with a trace id."""
    body = ErrorResponse(error=error, trace_id=trace_id, detail=detail).model_dump()
    return JSONResponse(status_code=status_code, content=body)


async def _authorize_patient_access(
    session: AsyncSession,
    *,
    patient_id: str,
    user: AuthenticatedUser,
    must_exist: bool = True,
) -> Optional[Patient]:
    """
    Ensure the authenticated user is allowed to touch this patient.

    Rules:
      * If the patient row exists and has a facility_id, it must match the
        caller's facility_id — or we 403.
      * If the patient exists with facility_id=NULL (legacy), we allow only
        if the caller's role allows cross-facility reads (none today; this
        is a hard-deny for caregivers).
      * If the patient does not exist and `must_exist` is True, we 404.

    This function never discloses WHICH check failed — caregivers fishing
    for valid patient_ids will see the same generic forbidden/not-found.
    """
    row = await session.scalar(
        select(Patient).where(Patient.patient_id == patient_id)
    )
    if row is None:
        if must_exist:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Patient not found",
            )
        return None

    row_facility = row.facility_id
    user_facility = user.facility_id

    # A caregiver without a facility claim cannot read any facility-scoped
    # patient — this protects against a mis-provisioned pool user.
    if row_facility is not None and row_facility != user_facility:
        logger.info(
            "authz: patient facility mismatch patient_id=%s user_id=%s",
            patient_id,
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )
    if row_facility is None:
        # Legacy / unassigned patient: treat as facility-pinned-to-nothing,
        # refuse caregivers.
        logger.info(
            "authz: patient has no facility_id patient_id=%s user_id=%s",
            patient_id,
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )

    return row


async def _ensure_patient_for_intake(
    session: AsyncSession,
    *,
    patient_id: str,
    user: AuthenticatedUser,
) -> None:
    """
    Ensure a Patient row exists for an intake run.

    Uses ON CONFLICT DO NOTHING so concurrent intakes for the same
    patient_id cannot race-insert two rows. After the upsert, re-select to
    verify the row's facility_id matches the caller — if another facility
    already owns this patient_id, we refuse rather than attaching to it.
    """
    stmt = (
        pg_insert(Patient)
        .values(
            patient_id=patient_id,
            facility_id=user.facility_id,
            assessment={},
            updated_by=user.id,
        )
        .on_conflict_do_nothing(index_elements=[Patient.patient_id])
    )
    try:
        await session.execute(stmt)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("intake: upsert of patient row failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    existing = await session.scalar(
        select(Patient).where(Patient.patient_id == patient_id)
    )
    if existing is None:
        # The insert was a no-op AND the row vanished — impossible under
        # ON CONFLICT DO NOTHING semantics, but refuse rather than proceed
        # with inconsistent state.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Patient row inconsistent",
        )
    if existing.facility_id != user.facility_id:
        logger.info(
            "intake: patient_id already belongs to a different facility "
            "patient_id=%s user_id=%s",
            patient_id,
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden",
        )


def _require_prefix(s3_key: str, expected_prefix: str) -> None:
    """Refuse an s3_key whose prefix does not match the pipeline's file kind."""
    if not s3_key.startswith(expected_prefix):
        # models.IntakeRequest / ReassessmentRequest already verified the
        # key is under S3_KEY_ALLOWED_PREFIXES; this check narrows it to
        # the exact kind (intake uses uploads/, reassessment uses audio/).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="s3_key prefix does not match the pipeline type",
        )


async def _enqueue_sqs(settings: Settings, payload: SQSMessage) -> None:
    """
    JSON-serialize an SQSMessage and send it to the configured queue.

    Wraps the sync boto3 call in `asyncio.to_thread`. Raises HTTPException
    on AWS error so callers can respond with a clean 503 without leaking
    the botocore error string. A 413 is raised when the serialized body
    exceeds the worker's accepted size — in practice this only trips on
    pathological resume submissions with many large edited values.
    """
    body = payload.model_dump_json()
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > _MAX_SQS_BODY_BYTES:
        # The worker refuses bodies over 128 KiB as poison. Refuse at the
        # producer so the caller gets a clear 413 instead of a silent
        # dead-letter later.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                "Submission is too large to queue; reduce the number or size "
                "of edited values."
            ),
        )

    client = get_sqs_client()

    def _send() -> None:
        client.send_message(
            QueueUrl=settings.SQS_QUEUE_URL,
            MessageBody=body,
        )

    try:
        await asyncio.to_thread(_send)
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "sqs: send_message failed code=%s run_id=%s",
            _client_error_code(exc),
            payload.run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Queue unavailable",
        ) from None


async def _mark_run_failed_after_enqueue_error(
    session: AsyncSession,
    *,
    run_id: str,
    reason: str,
) -> None:
    """
    Best-effort compensating update when SQS enqueue fails AFTER the
    pipeline_run row is committed.

    Any failure of this update is logged but swallowed — the original
    enqueue error is what the client sees, and an operator can reconcile
    stuck-queued rows out of band.
    """
    try:
        stmt = (
            update(PipelineRun)
            .where(
                and_(
                    PipelineRun.run_id == run_id,
                    PipelineRun.status == PIPELINE_STATUS_QUEUED,
                )
            )
            .values(
                status=PIPELINE_STATUS_FAILED,
                error=reason[:2000],
                completed_at=utc_now(),
            )
        )
        await session.execute(stmt)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("recovery: failed to mark run as failed run_id=%s", run_id)


def _generate_upload_key(patient_id: str, file_type: str) -> str:
    """Construct an S3 key under the correct prefix for the given file type."""
    ext = _UPLOAD_EXTENSION_BY_TYPE[file_type]
    prefix = _KEY_PREFIX_BY_TYPE[file_type]
    # uuid4 gives us an unguessable, collision-free object name per upload.
    return f"{prefix}{patient_id}/{uuid.uuid4().hex}.{ext}"


async def _generate_presigned_put(
    settings: Settings,
    *,
    s3_key: str,
    content_type: str,
) -> str:
    """
    Build a presigned PUT URL that requires SSE-KMS at upload time.

    Including SSE headers in `Params` binds them into the signature — the
    client's PUT request MUST send the matching headers or S3 rejects with
    403. This closes the "client uploads with no encryption header" gap.
    """
    client = get_s3_client()

    params = {
        "Bucket": settings.S3_BUCKET,
        "Key": s3_key,
        "ContentType": content_type,
        # Require SSE-KMS. The bucket's default-encryption policy is the
        # primary defence; this is a belt-and-suspenders pin at the object
        # level. Signed into the URL so it is non-bypassable.
        "ServerSideEncryption": "aws:kms",
    }

    def _sign() -> str:
        return client.generate_presigned_url(
            ClientMethod="put_object",
            Params=params,
            ExpiresIn=_UPLOAD_URL_TTL_SECONDS,
            HttpMethod="PUT",
        )

    try:
        return await asyncio.to_thread(_sign)
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "s3: presign PUT failed code=%s",
            _client_error_code(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage unavailable",
        ) from None


async def _generate_presigned_get(settings: Settings, *, s3_key: str) -> tuple[str, int]:
    """Build a presigned GET URL for a stored artefact. Returns (url, ttl)."""
    client = get_s3_client()
    ttl = settings.S3_PRESIGNED_EXPIRY

    def _sign() -> str:
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={
                "Bucket": settings.S3_BUCKET,
                "Key": s3_key,
            },
            ExpiresIn=ttl,
            HttpMethod="GET",
        )

    try:
        return await asyncio.to_thread(_sign), ttl
    except (BotoCoreError, ClientError) as exc:
        logger.error(
            "s3: presign GET failed code=%s",
            _client_error_code(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage unavailable",
        ) from None


# --------------------------------------------------------------------------- #
# Global exception handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Flatten Pydantic validation errors to a short, non-echoing message.

    FastAPI's default handler returns the offending input values, which can
    reflect PHI (e.g. an `edited_value` that happens to contain a transcript
    fragment). We return only the field names that failed.
    """
    try:
        fields = sorted({
            ".".join(str(part) for part in err.get("loc", ()))
            for err in exc.errors()
            if err.get("loc")
        })[:10]
    except Exception:  # noqa: BLE001 — defensive; do not leak the raw errors
        fields = []

    detail = "invalid fields: " + ", ".join(fields) if fields else "invalid request"
    return _error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="validation_error",
        detail=detail[:500],
        trace_id=_trace_id(request),
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Wrap HTTPException responses in ErrorResponse with a trace id."""
    if exc.status_code >= 500:
        logger.error(
            "http_exception status=%d detail=%s trace_id=%s",
            exc.status_code,
            exc.detail,
            _trace_id(request),
        )
    return _error_response(
        status_code=exc.status_code,
        error=f"http_{exc.status_code}",
        detail=str(exc.detail) if exc.detail else None,
        trace_id=_trace_id(request),
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch-all for unhandled exceptions. Never leak the exception text.

    The full stack trace is logged server-side via logger.exception; the
    client sees a uniform 500 with the trace_id they can quote in support.
    """
    logger.exception(
        "unhandled exception path=%s trace_id=%s",
        request.url.path,
        _trace_id(request),
    )
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error="internal_error",
        detail="An unexpected error occurred",
        trace_id=_trace_id(request),
    )


# --------------------------------------------------------------------------- #
# GET /health
# --------------------------------------------------------------------------- #
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
)
async def health() -> HealthResponse:
    """
    Liveness + readiness probe.

    Returns HealthResponse regardless of dependency state; callers
    (load balancers, uptime monitors) treat 200 as live. Dependency state
    is reported through the `status` field so ops can distinguish "up and
    serving" from "up but DB is sad".
    """
    settings = get_settings()

    db_ok = await check_db_connectivity()
    try:
        aws_probe = await asyncio.to_thread(verify_aws_connectivity)
    except Exception:  # noqa: BLE001 — probe must never crash /health
        aws_probe = {}

    # "degraded" if anything the app needs is sick, else "ok".
    degraded = not db_ok or (aws_probe and not all(aws_probe.values()))
    status_text = "degraded" if degraded else "ok"

    return HealthResponse(
        status=status_text,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )


# --------------------------------------------------------------------------- #
# POST /get-upload-url
# --------------------------------------------------------------------------- #
@app.post(
    "/get-upload-url",
    response_model=UploadUrlResponse,
    tags=["upload"],
)
async def get_upload_url(
    body: UploadUrlRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> UploadUrlResponse:
    """
    Return a presigned PUT URL the client can use to upload an asset.

    The SERVER chooses the S3 key — the client has no influence over the
    path, so an attacker cannot coerce the API into generating a URL that
    targets an unrelated patient's prefix. The key shape is
    `{prefix}/{patient_id}/{uuid}.{ext}` where prefix is chosen by
    file_type.
    """
    ext = _UPLOAD_EXTENSION_BY_TYPE[body.file_type]
    content_type = _CONTENT_TYPE_BY_EXT[ext]
    s3_key = _generate_upload_key(body.patient_id, body.file_type)

    upload_url = await _generate_presigned_put(
        settings,
        s3_key=s3_key,
        content_type=content_type,
    )

    logger.info(
        "upload_url issued file_type=%s patient_id=%s user_id=%s ttl=%d",
        body.file_type,
        body.patient_id,
        user.id,
        _UPLOAD_URL_TTL_SECONDS,
    )

    return UploadUrlResponse(
        upload_url=upload_url,
        s3_key=s3_key,
        expires_in=_UPLOAD_URL_TTL_SECONDS,
    )


# --------------------------------------------------------------------------- #
# POST /intake
# --------------------------------------------------------------------------- #
@app.post(
    "/intake",
    response_model=PipelineResponse,
    tags=["pipeline"],
)
async def intake(
    body: IntakeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PipelineResponse:
    """
    Start an intake pipeline.

    Side effects (committed atomically before SQS send):
      * UPSERT the Patient row with facility_id=user.facility_id.
      * INSERT a pipeline_runs row in status='queued'.

    After the DB commit, we enqueue an SQS message for the worker. If SQS
    send fails, we compensate by marking the run as 'failed' and return
    503 — the row remains in the DB for observability.
    """
    _require_prefix(body.s3_key, _KEY_PREFIX_BY_TYPE["pdf"])

    await _ensure_patient_for_intake(session, patient_id=body.patient_id, user=user)

    run_id = generate_run_id()
    run = PipelineRun(
        run_id=run_id,
        patient_id=body.patient_id,
        pipeline_type=PIPELINE_TYPE_INTAKE,
        status=PIPELINE_STATUS_QUEUED,
        user_id=user.id,
        s3_key=body.s3_key,
    )
    try:
        session.add(run)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("intake: failed to insert pipeline_runs row")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    message = SQSMessage(
        run_id=run_id,
        patient_id=body.patient_id,
        s3_key=body.s3_key,
        pipeline_type=PIPELINE_TYPE_INTAKE,
        user_id=user.id,
    )
    try:
        await _enqueue_sqs(settings, message)
    except HTTPException:
        await _mark_run_failed_after_enqueue_error(
            session, run_id=run_id, reason="queue_unavailable_at_enqueue"
        )
        raise

    logger.info(
        "intake enqueued run_id=%s patient_id=%s user_id=%s",
        run_id,
        body.patient_id,
        user.id,
    )

    return PipelineResponse(
        run_id=run_id,
        status=PIPELINE_STATUS_QUEUED,
        message="Intake pipeline queued.",
    )


# --------------------------------------------------------------------------- #
# POST /reassessment
# --------------------------------------------------------------------------- #
@app.post(
    "/reassessment",
    response_model=PipelineResponse,
    tags=["pipeline"],
)
async def reassessment(
    body: ReassessmentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PipelineResponse:
    """
    Start a reassessment pipeline.

    Unlike /intake this REQUIRES the patient row to already exist and to
    belong to the caller's facility — reassessment mutates an existing
    record and cross-facility access would be a data breach.
    """
    _require_prefix(body.s3_key, _KEY_PREFIX_BY_TYPE["audio"])

    await _authorize_patient_access(session, patient_id=body.patient_id, user=user)

    run_id = generate_run_id()
    run = PipelineRun(
        run_id=run_id,
        patient_id=body.patient_id,
        pipeline_type=PIPELINE_TYPE_REASSESSMENT,
        status=PIPELINE_STATUS_QUEUED,
        user_id=user.id,
        s3_key=body.s3_key,
    )
    try:
        session.add(run)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("reassessment: failed to insert pipeline_runs row")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    message = SQSMessage(
        run_id=run_id,
        patient_id=body.patient_id,
        s3_key=body.s3_key,
        pipeline_type=PIPELINE_TYPE_REASSESSMENT,
        user_id=user.id,
    )
    try:
        await _enqueue_sqs(settings, message)
    except HTTPException:
        await _mark_run_failed_after_enqueue_error(
            session, run_id=run_id, reason="queue_unavailable_at_enqueue"
        )
        raise

    logger.info(
        "reassessment enqueued run_id=%s patient_id=%s user_id=%s",
        run_id,
        body.patient_id,
        user.id,
    )

    return PipelineResponse(
        run_id=run_id,
        status=PIPELINE_STATUS_QUEUED,
        message="Reassessment pipeline queued.",
    )


# --------------------------------------------------------------------------- #
# GET /review/{patient_id}
# --------------------------------------------------------------------------- #
@app.get(
    "/review/{patient_id}",
    response_model=ReviewResponse,
    tags=["review"],
)
async def get_review(
    patient_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ReviewResponse:
    """
    Return the caregiver's pending review for this patient, if any.

    Enriches each flagged update with the `old_value` currently stored on
    the patient record so the UI can show a before/after diff without
    round-tripping the entire patient_json (which is PHI we would rather
    not emit over the wire more than necessary).
    """
    patient = await _authorize_patient_access(
        session, patient_id=patient_id, user=user
    )
    assert patient is not None  # _authorize_patient_access returns non-None here

    # The "pending" run is the most recent one in waiting_review for this
    # patient. There should be at most one, but we order and limit anyway
    # to be robust against a future bug where two overlap.
    pending = await session.scalar(
        select(PipelineRun)
        .where(
            and_(
                PipelineRun.patient_id == patient_id,
                PipelineRun.status == PIPELINE_STATUS_WAITING_REVIEW,
            )
        )
        .order_by(desc(PipelineRun.created_at))
        .limit(1)
    )

    if pending is None or not pending.flagged_updates:
        return ReviewResponse(pending=False)

    flagged = list(pending.flagged_updates)
    enriched: list[dict[str, Any]] = []
    for item in flagged:
        if not isinstance(item, dict):
            continue
        enriched_item = dict(item)
        field_path = item.get("field_path")
        if isinstance(field_path, str) and field_path and field_path != "AMBIGUOUS":
            try:
                enriched_item["old_value"] = get_nested(patient.assessment, field_path)
            except (TypeError, ValueError):
                # A malformed path cannot possibly exist on the record;
                # leave old_value absent rather than raising.
                enriched_item["old_value"] = None
        else:
            enriched_item["old_value"] = None
        enriched.append(enriched_item)

    return ReviewResponse(
        pending=True,
        run_id=pending.run_id,
        flagged_updates=enriched,
        patient_context=None,
    )


# --------------------------------------------------------------------------- #
# POST /review/{patient_id}
# --------------------------------------------------------------------------- #
@app.post(
    "/review/{patient_id}",
    response_model=PipelineResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["review"],
)
async def submit_review(
    body: ReviewSubmission,
    patient_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PipelineResponse:
    """
    Accept caregiver decisions and enqueue a RESUME job for the worker.

    This endpoint is explicitly async: it does NOT run the pipeline
    inline. Running the graph (merge → save_json → audit → generate_pdf)
    can take 2–30s once the PDF renderer lands, and holding a mobile
    client's HTTP connection that long is unreliable (carrier NATs,
    backgrounded apps, proxy timeouts). Instead we transition the run
    from 'waiting_review' to 'queued', enqueue the decisions to SQS, and
    return 202 Accepted. The worker picks up the message and resumes the
    graph via `Command(resume=...)`. When the run reaches a terminal
    state, the worker fires pipeline_complete (or pipeline_failed) over
    SNS so the client learns the outcome out-of-band — the same async
    contract /intake and /reassessment use.

    Idempotency and concurrency:
      * We perform a single CAS UPDATE from 'waiting_review' to 'queued'.
        If the UPDATE affects 0 rows, another submission already owns
        the resume and we 409. This prevents two concurrent caregivers
        from racing the resume.
      * The SQS enqueue happens AFTER the CAS commits. If SQS send
        fails, we mark the run 'failed' so ops can reconcile. The
        caregiver gets a 503 and the decisions are not silently
        swallowed into an unobservable state.
    """
    # Facility check on the patient; also 404s if the patient does not exist.
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    # Verify the run belongs to THIS patient. Without this, an attacker with
    # access to facility A could submit decisions for a waiting review on
    # facility B's patient using a leaked run_id.
    run = await session.scalar(
        select(PipelineRun).where(PipelineRun.run_id == body.run_id)
    )
    if run is None or run.patient_id != patient_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found for this patient",
        )
    if run.pipeline_type != PIPELINE_TYPE_REASSESSMENT:
        # Only reassessment runs go through human review; a client asking
        # to resume an intake with decisions is malformed.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Review is not applicable to this run",
        )
    if run.status != PIPELINE_STATUS_WAITING_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run is not awaiting review",
        )
    if not run.s3_key:
        # The original reassessment must have an s3_key; if it doesn't,
        # the run is corrupt and we refuse to propagate that state into
        # a resume message that SQSMessage would then reject.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Run is missing its audio key",
        )

    # CAS: only one caller flips waiting_review → queued. The worker
    # will do the queued → running transition when it picks the message
    # up, mirroring the fresh-run lifecycle.
    cas_stmt = (
        update(PipelineRun)
        .where(
            and_(
                PipelineRun.run_id == body.run_id,
                PipelineRun.status == PIPELINE_STATUS_WAITING_REVIEW,
            )
        )
        .values(status=PIPELINE_STATUS_QUEUED)
        .returning(PipelineRun.run_id)
    )
    try:
        cas_result = await session.execute(cas_stmt)
        if cas_result.first() is None:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Run is not awaiting review",
            )
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("review: CAS update failed run_id=%s", body.run_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    # Build the resume message. The model validator on SQSMessage enforces
    # pipeline_type='reassessment' + non-empty decisions when resume=True,
    # so an inconsistent shape is refused here rather than on the worker.
    message = SQSMessage(
        run_id=body.run_id,
        patient_id=patient_id,
        s3_key=run.s3_key,
        pipeline_type=PIPELINE_TYPE_REASSESSMENT,
        user_id=user.id,
        resume=True,
        decisions=list(body.decisions),
    )

    try:
        await _enqueue_sqs(settings, message)
    except HTTPException:
        # Compensate: the row is queued but the worker will never see the
        # message. Mark failed so operators can reconcile; reverting to
        # 'waiting_review' would let the client retry but the CAS has
        # already fired once and the caller is getting a 5xx anyway.
        await _mark_run_failed_after_enqueue_error(
            session,
            run_id=body.run_id,
            reason="queue_unavailable_at_review_resume",
        )
        raise

    logger.info(
        "review: resume queued run_id=%s patient_id=%s user_id=%s decisions=%d",
        body.run_id,
        patient_id,
        user.id,
        len(body.decisions),
    )

    return PipelineResponse(
        run_id=body.run_id,
        status=PIPELINE_STATUS_QUEUED,
        message="Resume queued",
    )


# --------------------------------------------------------------------------- #
# GET /patient/{patient_id}/status
# --------------------------------------------------------------------------- #
@app.get(
    "/patient/{patient_id}/status",
    response_model=PatientStatusResponse,
    tags=["patient"],
)
async def patient_status(
    patient_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PatientStatusResponse:
    """
    Return the latest pipeline run state for this patient.

    If the patient exists but has never run a pipeline, returns
    status='none' — distinguishable from 'queued' so the UI can prompt
    for an intake.
    """
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    latest = await session.scalar(
        select(PipelineRun)
        .where(PipelineRun.patient_id == patient_id)
        .order_by(desc(PipelineRun.created_at))
        .limit(1)
    )

    if latest is None:
        return PatientStatusResponse(
            patient_id=patient_id,
            run_id=None,
            status="none",
        )

    return PatientStatusResponse(
        patient_id=patient_id,
        run_id=latest.run_id,
        status=latest.status,  # type: ignore[arg-type]
        started_at=latest.started_at,
        completed_at=latest.completed_at,
        error=latest.error,
        has_pending_review=(latest.status == PIPELINE_STATUS_WAITING_REVIEW),
    )


# --------------------------------------------------------------------------- #
# GET /get-download-url/{patient_id}
# --------------------------------------------------------------------------- #
@app.get(
    "/get-download-url/{patient_id}",
    response_model=DownloadUrlResponse,
    tags=["download"],
)
async def get_download_url(
    patient_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DownloadUrlResponse:
    """
    Return a short-TTL presigned GET URL for the most recent completed
    pipeline run's output PDF.

    NOTE: `generate_pdf` is currently a placeholder that stores no PDF —
    every completed run will have output_pdf_s3_key=NULL and this endpoint
    will 404 until the real renderer lands. The 404 is preferred over a
    silent stub URL because a caregiver would waste time opening a broken
    link.
    """
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    latest = await session.scalar(
        select(PipelineRun)
        .where(
            and_(
                PipelineRun.patient_id == patient_id,
                PipelineRun.status == PIPELINE_STATUS_COMPLETE,
                PipelineRun.output_pdf_s3_key.isnot(None),
            )
        )
        .order_by(desc(PipelineRun.created_at))
        .limit(1)
    )

    if latest is None or not latest.output_pdf_s3_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No generated PDF is available for this patient yet",
        )

    url, ttl = await _generate_presigned_get(
        settings, s3_key=latest.output_pdf_s3_key
    )

    logger.info(
        "download_url issued patient_id=%s run_id=%s user_id=%s ttl=%d",
        patient_id,
        latest.run_id,
        user.id,
        ttl,
    )

    return DownloadUrlResponse(
        download_url=url,
        expires_in=ttl,
        generated_at=utc_now(),
    )


__all__ = ["app"]
