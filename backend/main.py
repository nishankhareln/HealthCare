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

from auth import (
    AuthenticatedUser,
    close_http_client,
    get_current_user,
    issue_access_token,
    verify_password,
)
from aws_clients import (
    _client_error_code,
    get_s3_client,
    get_sqs_client,
    verify_aws_connectivity,
)
from config import Settings, get_settings
from database import check_db_connectivity, get_db, init_db, shutdown_db
from db_models import (
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_WAITING_REVIEW,
    PIPELINE_TYPE_INTAKE,
    PIPELINE_TYPE_INTAKE_DOCTOR,
    PIPELINE_TYPE_REASSESSMENT,
    Patient,
    PipelineRun,
)
from db_models import APPROVAL_METHOD_HUMAN, AuditEntry, User
from models import (
    AuthMeResponse,
    LoginRequest,
    LoginResponse,
    DownloadUrlResponse,
    ErrorResponse,
    HealthResponse,
    IntakeRequest,
    PatientCreateRequest,
    PatientDetailResponse,
    PatientListItem,
    PatientListResponse,
    PatientPatchRequest,
    PatientStatusResponse,
    PipelineResponse,
    ReassessmentRequest,
    ReviewResponse,
    ReviewSubmission,
    SignedPdfRequest,
    SQSMessage,
    UploadUrlRequest,
    UploadUrlResponse,
)
from pipeline import (
    init_pipeline,
    shutdown_pipeline,
)
from utils import generate_run_id, get_nested, set_nested, utc_now


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
    # Flutter records audio on-device and uploads it here. The backend's
    # transcribe node feeds it to Amazon Transcribe Medical.
    "audio": "m4a",
    # Flutter renders + signs the WAC-388-76-615 template on-device, then
    # uploads the signed PDF here for the legal archive.
    "signed_pdf": "pdf",
}
_CONTENT_TYPE_BY_EXT: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "m4a": "audio/mp4",
}
_KEY_PREFIX_BY_TYPE: Final[dict[str, str]] = {
    "pdf": "uploads/",
    "audio": "audio/",
    "signed_pdf": "signed/",
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
# POST /intake/doctor
# --------------------------------------------------------------------------- #
@app.post(
    "/intake/doctor",
    response_model=PipelineResponse,
    tags=["pipeline"],
)
async def intake_doctor(
    body: IntakeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PipelineResponse:
    """
    Start a doctor-PDF intake pipeline.

    Caregiver uploads a doctor-supplied PDF (~15 pages, ~6 pages of useful
    content) covering the resident's medical conditions, diagnoses, and
    medications. This endpoint queues a `intake_doctor` pipeline run that:

      * Parses the PDF using Bedrock Sonnet 4.5 with a doctor-PDF-specific
        prompt that extracts ONLY the medical schema fields.
      * Deep-merges the medical extraction into the existing
        `patients.assessment` JSON, preserving caregiver-owned fields
        (mobility, ADLs, etc.).
      * Writes one audit_trail row per CHANGED medical field.

    Unlike /intake this REQUIRES the patient row to already exist —
    a doctor PDF is only meaningful for an enrolled resident. We do NOT
    upsert the patient here.
    """
    _require_prefix(body.s3_key, _KEY_PREFIX_BY_TYPE["pdf"])

    await _authorize_patient_access(
        session, patient_id=body.patient_id, user=user
    )

    run_id = generate_run_id()
    run = PipelineRun(
        run_id=run_id,
        patient_id=body.patient_id,
        pipeline_type=PIPELINE_TYPE_INTAKE_DOCTOR,
        status=PIPELINE_STATUS_QUEUED,
        user_id=user.id,
        s3_key=body.s3_key,
    )
    try:
        session.add(run)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("intake_doctor: failed to insert pipeline_runs row")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    message = SQSMessage(
        run_id=run_id,
        patient_id=body.patient_id,
        s3_key=body.s3_key,
        pipeline_type=PIPELINE_TYPE_INTAKE_DOCTOR,
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
        "intake_doctor enqueued run_id=%s patient_id=%s user_id=%s",
        run_id,
        body.patient_id,
        user.id,
    )

    return PipelineResponse(
        run_id=run_id,
        status=PIPELINE_STATUS_QUEUED,
        message="Doctor PDF intake pipeline queued.",
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

    Flutter records the dictation on-device and uploads the audio file
    to S3 under the `audio/` prefix. The backend's transcribe node calls
    Amazon Transcribe Medical against that audio.
    """
    _require_prefix(body.audio_s3_key, _KEY_PREFIX_BY_TYPE["audio"])

    await _authorize_patient_access(session, patient_id=body.patient_id, user=user)

    run_id = generate_run_id()
    run = PipelineRun(
        run_id=run_id,
        patient_id=body.patient_id,
        pipeline_type=PIPELINE_TYPE_REASSESSMENT,
        status=PIPELINE_STATUS_QUEUED,
        user_id=user.id,
        s3_key=body.audio_s3_key,
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
        s3_key=body.audio_s3_key,
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
    Return a short-TTL presigned GET URL for the most recent SIGNED PDF.

    Flutter renders the WAC-388-76-615 care-plan template on-device from the
    patient JSON, captures the caregiver's signature, and uploads the signed
    PDF via POST /signed-pdf/{run_id}. This endpoint serves that signed
    artifact back to authorized callers (e.g., for emailing to the case
    manager or printing for the chart).

    Returns 404 if no signed PDF has been uploaded for this patient yet —
    the backend never generates PDFs itself.
    """
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    latest = await session.scalar(
        select(PipelineRun)
        .where(
            and_(
                PipelineRun.patient_id == patient_id,
                PipelineRun.signed_pdf_s3_key.isnot(None),
            )
        )
        .order_by(desc(PipelineRun.signed_at))
        .limit(1)
    )

    if latest is None or not latest.signed_pdf_s3_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No signed PDF is available for this patient yet",
        )

    url, ttl = await _generate_presigned_get(
        settings, s3_key=latest.signed_pdf_s3_key
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


# --------------------------------------------------------------------------- #
# POST /signed-pdf/{run_id}
# --------------------------------------------------------------------------- #
@app.post(
    "/signed-pdf/{run_id}",
    status_code=status.HTTP_200_OK,
    tags=["review"],
)
async def register_signed_pdf(
    body: SignedPdfRequest,
    run_id: str = Path(..., min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Register a Flutter-uploaded signed PDF against a completed pipeline run.

    Flow:
      1. Flutter renders the WAC-388-76-615 template from the patient JSON
         and captures the caregiver's signature.
      2. Flutter calls POST /get-upload-url with upload_type='signed_pdf'
         and uploads the rendered bytes directly to S3.
      3. Flutter calls THIS endpoint with the returned S3 key and the
         template_version identifier.
      4. Backend verifies facility-scoped access to the run's patient,
         records the S3 key + signed_at on the pipeline_runs row, and
         stamps template_version on the patient record.

    Idempotent — if `signed_pdf_s3_key` is already set to the same value,
    we accept; if it's set to a different value, we replace (latest signed
    copy wins, with the audit trail recording the version history via
    `signed_at`).
    """
    run = await session.scalar(
        select(PipelineRun).where(PipelineRun.run_id == run_id)
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pipeline run not found",
        )

    await _authorize_patient_access(
        session, patient_id=run.patient_id, user=user
    )

    # The caller must have asked for an upload URL of the right type;
    # _validate_s3_key on SignedPdfRequest already enforces the 'signed/'
    # prefix, but we re-check here in case the validator is ever relaxed.
    _require_prefix(body.s3_key, _KEY_PREFIX_BY_TYPE["signed_pdf"])

    now = utc_now()

    await session.execute(
        update(PipelineRun)
        .where(PipelineRun.run_id == run_id)
        .values(signed_pdf_s3_key=body.s3_key, signed_at=now)
    )
    await session.execute(
        update(Patient)
        .where(Patient.patient_id == run.patient_id)
        .values(template_version=body.template_version)
    )
    await session.commit()

    logger.info(
        "signed_pdf registered run_id=%s patient_id=%s user_id=%s "
        "template_version=%s",
        run_id,
        run.patient_id,
        user.id,
        body.template_version,
    )

    return {
        "run_id": run_id,
        "patient_id": run.patient_id,
        "signed_at": now,
        "status": "registered",
    }


# --------------------------------------------------------------------------- #
# POST /auth/login
# --------------------------------------------------------------------------- #
@app.post(
    "/auth/login",
    response_model=LoginResponse,
    tags=["auth"],
)
async def auth_login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """
    Verify username + password against the `users` table and issue a JWT.

    Security posture:
      * Password is bcrypt-verified — constant-time comparison via passlib.
      * Failure case never tells the caller WHICH part was wrong (no
        "user exists / wrong password" oracle). One uniform 401.
      * On success, we update `last_login_at` for operator visibility.
      * We never log the password, never log the issued token, never log
        the password hash. Username and user_id only.
    """
    # Same lookup whether or not the user exists — keeps timing close to
    # constant. We still call verify_password against the FAKE hash
    # below so attackers cannot easily distinguish "no such user" from
    # "wrong password" by response time alone.
    user_row = await session.scalar(
        select(User).where(User.username == body.username)
    )

    if user_row is None:
        # Compare the supplied password against a known-bad hash so the
        # CPU cost of a missed-username path approximately matches the
        # cost of a wrong-password path.
        verify_password(body.password, "$2b$12$invalidinvalidinvalidinvalidinvalidinvalidinvalidinvalidinva")
        logger.info("auth_login: failed (no such user) attempt_username=%s", body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    if not verify_password(body.password, user_row.password_hash):
        logger.info("auth_login: failed (bad password) user_id=%s", user_row.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token, expires_in = issue_access_token(
        user_id=user_row.id,
        username=user_row.username,
        settings=settings,
    )

    user_row.last_login_at = utc_now()
    try:
        await session.commit()
    except SQLAlchemyError:
        # Login still succeeded — failing to record `last_login_at`
        # should not break the user's session. Roll back the timestamp
        # write only.
        await session.rollback()
        logger.warning("auth_login: failed to record last_login_at user_id=%s", user_row.id)

    logger.info("auth_login: success user_id=%s", user_row.id)

    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
        user=AuthMeResponse(id=user_row.id, username=user_row.username),
    )


# --------------------------------------------------------------------------- #
# GET /auth/me
# --------------------------------------------------------------------------- #
@app.get(
    "/auth/me",
    response_model=AuthMeResponse,
    tags=["auth"],
)
async def auth_me(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthMeResponse:
    """
    Return the authenticated caller's identity for UI display.

    Note we never expose the raw JWT or any claim outside the small set
    of fields below — token contents stay server-side.
    """
    return AuthMeResponse(id=user.id, username=user.username)


# --------------------------------------------------------------------------- #
# POST /patients
# --------------------------------------------------------------------------- #
@app.post(
    "/patients",
    response_model=PatientDetailResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["patients"],
)
async def create_patient(
    body: PatientCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PatientDetailResponse:
    """
    Create an empty patient record.

    Caregiver supplies the ACES ID + preferred name. The assessment JSON
    starts empty; it gets populated later by an /intake or /reassessment
    run, or directly via PATCH for caregiver edits.

    Facility defaults to the caller's facility unless explicitly set —
    cross-facility creation requires the explicit facility_id and is
    typically denied at the authz layer (caller can only act within
    their own facility).
    """
    # Caller's own facility unless they explicitly named one. We do not
    # let a caller "claim" a patient into a facility that isn't theirs.
    facility = body.facility_id or user.facility_id
    if facility and user.facility_id and facility != user.facility_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create a patient outside your facility",
        )

    # Conflict-safe insert: if the patient_id already exists (and isn't
    # archived), 409. If it exists archived, also 409 with a clearer
    # message — admin must un-archive deliberately, not by re-create.
    existing = await session.scalar(
        select(Patient).where(Patient.patient_id == body.patient_id)
    )
    if existing is not None:
        if existing.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Patient exists but is archived. Un-archive via admin "
                    "tooling rather than re-creating."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Patient with this ACES ID already exists",
        )

    new_patient = Patient(
        patient_id=body.patient_id,
        preferred_name=body.preferred_name,
        facility_id=facility,
        assessment={},
        updated_by=user.id,
    )
    try:
        session.add(new_patient)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("create_patient: insert failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    await session.refresh(new_patient)

    logger.info(
        "patient created patient_id=%s facility=%s user_id=%s",
        body.patient_id,
        facility,
        user.id,
    )

    return PatientDetailResponse(
        patient_id=new_patient.patient_id,
        preferred_name=new_patient.preferred_name,
        facility_id=new_patient.facility_id,
        assessment=new_patient.assessment,
        created_at=new_patient.created_at,
        updated_at=new_patient.updated_at,
        updated_by=new_patient.updated_by,
        template_version=new_patient.template_version,
    )


# --------------------------------------------------------------------------- #
# GET /patients
# --------------------------------------------------------------------------- #
_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200


@app.get(
    "/patients",
    response_model=PatientListResponse,
    tags=["patients"],
)
async def list_patients(
    limit: int = _LIST_LIMIT_DEFAULT,
    offset: int = 0,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PatientListResponse:
    """
    Paginated list of patients in the caller's facility.

    Excludes archived patients. The response is deliberately slim — no
    assessment JSON. Flutter calls GET /patients/{patient_id} to load
    the full record when the caregiver opens a patient.
    """
    if limit < 1 or limit > _LIST_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"limit must be between 1 and {_LIST_LIMIT_MAX}",
        )
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset must be non-negative",
        )

    base = select(Patient).where(Patient.archived_at.is_(None))
    if user.facility_id:
        base = base.where(Patient.facility_id == user.facility_id)

    # Count first so the client knows how many pages exist.
    from sqlalchemy import func as sa_func  # local import — used only here
    count_stmt = base.with_only_columns(sa_func.count(Patient.patient_id))
    total = (await session.execute(count_stmt)).scalar_one()

    rows = (
        await session.execute(
            base.order_by(desc(Patient.updated_at), desc(Patient.created_at))
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    # Latest run status per patient — one query, indexed.
    items: list[PatientListItem] = []
    if rows:
        patient_ids = [r.patient_id for r in rows]
        latest_runs = (
            await session.execute(
                select(
                    PipelineRun.patient_id,
                    PipelineRun.status,
                    PipelineRun.created_at,
                )
                .where(PipelineRun.patient_id.in_(patient_ids))
                .order_by(PipelineRun.patient_id, desc(PipelineRun.created_at))
            )
        ).all()
        latest_status_by_patient: dict[str, str] = {}
        for pid, st, _ in latest_runs:
            if pid not in latest_status_by_patient:
                latest_status_by_patient[pid] = st

        for r in rows:
            items.append(
                PatientListItem(
                    patient_id=r.patient_id,
                    preferred_name=r.preferred_name,
                    facility_id=r.facility_id,
                    updated_at=r.updated_at,
                    latest_run_status=latest_status_by_patient.get(r.patient_id),
                )
            )

    return PatientListResponse(
        items=items,
        total=int(total),
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------- #
# GET /patients/{patient_id}
# --------------------------------------------------------------------------- #
@app.get(
    "/patients/{patient_id}",
    response_model=PatientDetailResponse,
    tags=["patients"],
)
async def get_patient(
    patient_id: str = Path(..., min_length=9, max_length=9, pattern=r"^\d{9}$"),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PatientDetailResponse:
    """
    Full patient record including the assessment JSON.

    Returns 404 if archived or outside the caller's facility — we do not
    distinguish "doesn't exist" from "you can't see it" to avoid leaking
    membership of the patient population.
    """
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    row = await session.scalar(
        select(Patient).where(
            and_(
                Patient.patient_id == patient_id,
                Patient.archived_at.is_(None),
            )
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found"
        )

    return PatientDetailResponse(
        patient_id=row.patient_id,
        preferred_name=row.preferred_name,
        facility_id=row.facility_id,
        assessment=row.assessment,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        template_version=row.template_version,
    )


# --------------------------------------------------------------------------- #
# PATCH /patients/{patient_id}
# --------------------------------------------------------------------------- #
@app.patch(
    "/patients/{patient_id}",
    response_model=PatientDetailResponse,
    tags=["patients"],
)
async def patch_patient(
    body: PatientPatchRequest,
    patient_id: str = Path(..., min_length=9, max_length=9, pattern=r"^\d{9}$"),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PatientDetailResponse:
    """
    Caregiver-driven correction to a patient record.

    Two kinds of updates can ride on a single PATCH:
      * Top-level metadata (`preferred_name`, `facility_id`).
      * Field-level edits to the assessment JSON via `assessment_patch`,
        a flat `{ "dotted.path": new_value }` map.

    Every changed assessment field writes one append-only `audit_trail`
    row with `approval_method='human'` so the change is attributable to
    the caregiver, not the LLM. The previous value is captured as the
    audit `old_value`.

    A run_id is generated for the PATCH itself so caregiver edits are
    traceable as a synthetic "manual" run in the audit history.
    """
    await _authorize_patient_access(session, patient_id=patient_id, user=user)

    if (
        body.preferred_name is None
        and body.facility_id is None
        and not body.assessment_patch
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of preferred_name, facility_id, "
            "or assessment_patch must be supplied",
        )

    row = await session.scalar(
        select(Patient).where(
            and_(
                Patient.patient_id == patient_id,
                Patient.archived_at.is_(None),
            )
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found"
        )

    # Defence-in-depth: if a caller tries to move a patient into a foreign
    # facility, refuse. Same posture as create_patient.
    if (
        body.facility_id is not None
        and user.facility_id
        and body.facility_id != user.facility_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot reassign a patient to a different facility",
        )

    # Apply assessment patches into a deep-copied dict — we want to
    # preserve the original until the audit rows are queued.
    patch_run_id = generate_run_id()
    audit_rows: list[AuditEntry] = []

    new_assessment: dict[str, Any] = copy.deepcopy(row.assessment) if row.assessment else {}
    if body.assessment_patch:
        for field_path, new_value in body.assessment_patch.items():
            old_value = get_nested(new_assessment, field_path)
            if old_value == new_value:
                continue  # no-op edit
            try:
                set_nested(new_assessment, field_path, new_value)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid assessment_patch path: "
                    f"{type(exc).__name__}",
                )
            audit_rows.append(
                AuditEntry(
                    patient_id=patient_id,
                    run_id=patch_run_id,
                    field_path=field_path,
                    old_value=json.dumps(old_value, default=str)
                    if old_value is not None
                    else None,
                    new_value=json.dumps(new_value, default=str),
                    source_phrase=None,
                    confidence=None,
                    user_id=user.id,
                    approval_method=APPROVAL_METHOD_HUMAN,
                )
            )

    # Apply scalar field updates.
    if body.preferred_name is not None:
        row.preferred_name = body.preferred_name
    if body.facility_id is not None:
        row.facility_id = body.facility_id
    if body.assessment_patch:
        row.assessment = new_assessment
    row.updated_by = user.id

    try:
        if audit_rows:
            session.add_all(audit_rows)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("patch_patient: commit failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )

    await session.refresh(row)

    logger.info(
        "patient patched patient_id=%s user_id=%s field_changes=%d",
        patient_id,
        user.id,
        len(audit_rows),
    )

    return PatientDetailResponse(
        patient_id=row.patient_id,
        preferred_name=row.preferred_name,
        facility_id=row.facility_id,
        assessment=row.assessment,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        template_version=row.template_version,
    )


__all__ = ["app"]
