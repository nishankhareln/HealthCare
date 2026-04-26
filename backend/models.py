"""
Pydantic models for API requests/responses and internal message shapes.

This module is the PRIMARY validation boundary for the backend. Every byte
that enters the system from an HTTP client, an SQS message, or an LLM
response is parsed through one of the models defined here. Defensive
validation here is the main reason downstream code (database, AWS calls,
pipeline nodes) can trust its inputs.

Security posture:
  * `extra="forbid"` on every model — unknown fields are rejected, not
    silently ignored. This prevents an attacker from smuggling extra keys
    past a lenient parser.
  * String fields carry BOTH a `min_length` (reject empty strings that can
    bypass downstream "if x:" checks) and a `max_length` (reject megabyte
    payloads at the validation layer so they never reach DB, LLM, or S3).
  * `patient_id`, `run_id`, and `field_path` are constrained by regex
    patterns. Patterns are intentionally simple — no backtracking
    constructs — to avoid ReDoS.
  * `s3_key` is validated against path traversal, embedded NULs, control
    characters, and an allow-list of prefixes. A client can never coerce
    the server into generating a presigned URL for an arbitrary key.
  * `edited_value` in review submissions is size-bounded (16 KiB JSON)
    because it's the one place an authenticated client can push arbitrary
    JSON into the database.
  * `confidence` is strictly `[0.0, 1.0]` — prevents poisoning downstream
    routing with out-of-range scores from a compromised LLM response.
  * `SQSMessage` is re-validated by the worker before acting; the worker
    treats SQS payloads as untrusted input just like HTTP bodies.
  * No PHI lives in this file (only structure, constraints, and enums).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Literal enums (mirror db_models CHECK constraints)
# --------------------------------------------------------------------------- #
FileType = Literal["pdf", "audio", "signed_pdf"]
ReviewAction = Literal["approve", "reject", "edit"]
PipelineType = Literal["intake", "intake_doctor", "reassessment"]
PipelineStatus = Literal[
    "queued", "running", "waiting_review", "complete", "failed"
]
ApprovalMethod = Literal["auto", "human"]


# --------------------------------------------------------------------------- #
# Shared constraints
# --------------------------------------------------------------------------- #
# Identifiers: alphanumeric, hyphen, underscore; must start with alphanumeric.
# 1-64 characters. Strict enough to rule out path-segment smuggling, loose
# enough to accept UUIDs, Cognito sub claims, and facility-local IDs.
PATIENT_ID_PATTERN = r"^\d{9}$"
PATIENT_ID_MAX_LEN = 9

# UUID v4 format (8-4-4-4-12 hex). Our own code only produces UUIDs, so we
# reject anything else at the boundary.
RUN_ID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Field path: either the literal "AMBIGUOUS" sentinel or a lowercase dotted
# path (segments are [a-z0-9_]+, each must start with a letter/digit).
FIELD_PATH_PATTERN = (
    r"^(?:AMBIGUOUS|[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)$"
)
FIELD_PATH_MAX_LEN = 255

USER_ID_MAX_LEN = 128

# S3 keys: max 1024 bytes per AWS spec. Must start with one of our known
# prefixes — the server NEVER generates or accepts keys outside these.
S3_KEY_MAX_LEN = 1024
S3_KEY_ALLOWED_PREFIXES: tuple[str, ...] = (
    "uploads/",
    "audio/",
    "transcripts/",
    "pdfs/",
    "signed/",
)

# Bounds on review submission
MAX_DECISIONS_PER_SUBMISSION = 500
MAX_EDITED_VALUE_JSON_BYTES = 16 * 1024  # 16 KiB
MAX_EDITED_VALUE_STR_LEN = 4096

# Bounds on LLM-derived free text
MAX_SOURCE_PHRASE_LEN = 2000
MAX_REASONING_LEN = 2000


# --------------------------------------------------------------------------- #
# Shared validator helpers
# --------------------------------------------------------------------------- #
def _validate_s3_key(value: str) -> str:
    """
    Defend against every common path-traversal / injection trick for S3 keys.

    Raises `ValueError` with a short message — Pydantic wraps it into a
    proper 422 response. The message does NOT echo the offending value,
    so a malicious input can't use the error response as a reflection
    oracle.
    """
    if not value:
        raise ValueError("s3_key must not be empty")
    if len(value) > S3_KEY_MAX_LEN:
        raise ValueError(f"s3_key exceeds {S3_KEY_MAX_LEN} characters")
    if value.startswith("/"):
        raise ValueError("s3_key must not start with '/'")
    if ".." in value:
        raise ValueError("s3_key must not contain '..'")
    if "//" in value:
        raise ValueError("s3_key must not contain '//'")
    if "\x00" in value:
        raise ValueError("s3_key must not contain NUL bytes")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("s3_key must not contain control characters")
    if not any(value.startswith(p) for p in S3_KEY_ALLOWED_PREFIXES):
        raise ValueError(
            "s3_key must start with one of the allowed prefixes "
            f"{S3_KEY_ALLOWED_PREFIXES}"
        )
    return value


def _validate_edited_value(value: Any) -> Any:
    """
    Bound the size of `edited_value` in review submissions.

    The review endpoint is the one place an authenticated caregiver can push
    arbitrary JSON into the patient record. We cap the serialized size so a
    single request cannot stuff the database with a multi-megabyte blob, and
    so the value can be safely logged (as counts) without memory blowups.
    """
    if value is None:
        return value
    if isinstance(value, str) and len(value) > MAX_EDITED_VALUE_STR_LEN:
        raise ValueError(
            f"edited_value string exceeds {MAX_EDITED_VALUE_STR_LEN} characters"
        )
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        # Don't leak the raw value in the error — just the exception type.
        raise ValueError(
            f"edited_value is not JSON-serializable: {type(exc).__name__}"
        ) from None
    if len(encoded.encode("utf-8")) > MAX_EDITED_VALUE_JSON_BYTES:
        raise ValueError(
            f"edited_value JSON size exceeds {MAX_EDITED_VALUE_JSON_BYTES} bytes"
        )
    return value


# --------------------------------------------------------------------------- #
# Base class — uniform config for every model in the module
# --------------------------------------------------------------------------- #
class _BaseModel(BaseModel):
    """
    Shared Pydantic v2 config.

    * `extra="forbid"` — unknown fields at any depth cause a 422 error.
    * `str_strip_whitespace=True` — inputs like "  abc  " are normalized
       before length/pattern checks, eliminating a bypass class.
    * `populate_by_name=False` — request parsing is key-exact; no aliases
       accepted without an explicit `Field(alias=...)`.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=False,
        arbitrary_types_allowed=False,
    )


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class UploadUrlRequest(_BaseModel):
    """POST /get-upload-url request body."""

    patient_id: str = Field(
        ...,
        min_length=1,
        max_length=PATIENT_ID_MAX_LEN,
        pattern=PATIENT_ID_PATTERN,
        description="Patient identifier. Alphanumeric + `-_`, must start with alphanumeric.",
    )
    file_type: FileType = Field(
        ...,
        description="Kind of file the client intends to upload.",
    )


class IntakeRequest(_BaseModel):
    """POST /intake request body."""

    patient_id: str = Field(
        ...,
        min_length=1,
        max_length=PATIENT_ID_MAX_LEN,
        pattern=PATIENT_ID_PATTERN,
    )
    s3_key: str = Field(
        ...,
        min_length=1,
        max_length=S3_KEY_MAX_LEN,
        description="S3 object key previously returned by POST /get-upload-url.",
    )

    @field_validator("s3_key")
    @classmethod
    def _check_s3_key(cls, v: str) -> str:
        return _validate_s3_key(v)


# --------------------------------------------------------------------------- #
# Patient CRUD
# --------------------------------------------------------------------------- #
class PatientCreateRequest(_BaseModel):
    """
    POST /patients body.

    Caregiver creates a new patient by entering the ACES ID + preferred
    name. The full assessment JSON is populated later by an /intake run
    or by /reassessment dictations. We accept an empty assessment shell
    here on purpose — it lets the patient appear on the home-screen list
    before any documents have been uploaded.
    """

    patient_id: str = Field(
        ...,
        min_length=PATIENT_ID_MAX_LEN,
        max_length=PATIENT_ID_MAX_LEN,
        pattern=PATIENT_ID_PATTERN,
        description="9-digit Washington ACES ID (e.g. '000333000').",
    )
    preferred_name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Display name for UI lists. Not the legal name.",
    )
    facility_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Facility this patient belongs to. Defaults to caller's.",
    )


class PatientPatchRequest(_BaseModel):
    """
    PATCH /patients/{patient_id} body.

    Partial update to the assessment JSON. `assessment_patch` is a flat
    dict of `dotted.field.path -> new_value`, intentionally identical to
    the format used by the /review endpoint — every changed field is
    audited.

    `preferred_name` and `facility_id` can be updated independently; they
    are NOT inside the assessment JSON.
    """

    preferred_name: Optional[str] = Field(default=None, max_length=128)
    facility_id: Optional[str] = Field(default=None, max_length=64)
    assessment_patch: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Flat map of dotted field paths to new values. "
            "e.g. {'demographics.dob': '5/9/1965'}"
        ),
    )

    @field_validator("assessment_patch")
    @classmethod
    def _check_patch(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if value is None:
            return value
        if len(value) == 0:
            raise ValueError("assessment_patch must not be empty")
        if len(value) > MAX_DECISIONS_PER_SUBMISSION:
            raise ValueError(
                f"assessment_patch must have at most "
                f"{MAX_DECISIONS_PER_SUBMISSION} entries"
            )
        for path, new_val in value.items():
            if not isinstance(path, str) or not path:
                raise ValueError("assessment_patch keys must be non-empty strings")
            if len(path) > FIELD_PATH_MAX_LEN:
                raise ValueError(
                    f"assessment_patch key exceeds {FIELD_PATH_MAX_LEN} chars"
                )
            if ".." in path or path.startswith(".") or path.endswith("."):
                raise ValueError(
                    "assessment_patch keys must be valid dotted paths"
                )
            _validate_edited_value(new_val)
        return value


class PatientListItem(_BaseModel):
    """One row in the GET /patients list response. Slim — no full JSON."""

    patient_id: str
    preferred_name: Optional[str] = None
    facility_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    latest_run_status: Optional[str] = None


class PatientListResponse(_BaseModel):
    """GET /patients response — paginated list of patients."""

    items: list[PatientListItem]
    total: int
    limit: int
    offset: int


class PatientDetailResponse(_BaseModel):
    """GET /patients/{patient_id} response — full record."""

    patient_id: str
    preferred_name: Optional[str] = None
    facility_id: Optional[str] = None
    assessment: dict[str, Any]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    template_version: Optional[str] = None


class AuthMeResponse(_BaseModel):
    """GET /auth/me response — the authenticated caller, for UI display."""

    id: str
    username: str


class LoginRequest(_BaseModel):
    """POST /auth/login body."""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)


class LoginResponse(_BaseModel):
    """POST /auth/login response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds.")
    user: AuthMeResponse


class ReassessmentRequest(_BaseModel):
    """
    POST /reassessment request body.

    Flutter records the dictation on-device and uploads the audio file
    to S3 under the `audio/` prefix. The backend's transcribe node then
    calls Amazon Transcribe Medical against that S3 object. Only the
    audio S3 key travels through this endpoint.
    """

    patient_id: str = Field(
        ...,
        min_length=1,
        max_length=PATIENT_ID_MAX_LEN,
        pattern=PATIENT_ID_PATTERN,
    )
    audio_s3_key: str = Field(
        ...,
        min_length=1,
        max_length=S3_KEY_MAX_LEN,
        description=(
            "S3 key of the Flutter-uploaded audio file. Must start "
            "with 'audio/'."
        ),
    )

    @field_validator("audio_s3_key")
    @classmethod
    def _check_audio_s3_key(cls, v: str) -> str:
        v = _validate_s3_key(v)
        if not v.startswith("audio/"):
            raise ValueError("audio_s3_key must start with 'audio/'")
        return v


class SignedPdfRequest(_BaseModel):
    """
    POST /signed-pdf/{run_id} request body.

    Flutter fills + signs the care-plan template on-device, then uploads the
    rendered PDF to S3 via a presigned URL (upload_type='signed_pdf'), then
    calls this endpoint with the returned S3 key. The backend stores the key
    on the corresponding pipeline_runs row — it becomes the legal archive
    artifact for that run.
    """

    s3_key: str = Field(
        ...,
        min_length=1,
        max_length=S3_KEY_MAX_LEN,
        description="S3 key of the signed PDF. Must start with 'signed/'.",
    )
    template_version: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Identifier of the Flutter-bundled template version used to render "
            "this signed PDF (e.g. 'WAC-388-76-615-v2024'). Stored so any "
            "regeneration can pick the exact same template."
        ),
    )

    @field_validator("s3_key")
    @classmethod
    def _check_s3_key(cls, v: str) -> str:
        v = _validate_s3_key(v)
        if not v.startswith("signed/"):
            raise ValueError("s3_key must start with 'signed/' for signed PDFs")
        return v


class ReviewDecision(_BaseModel):
    """Single caregiver decision inside a review submission."""

    update_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Opaque identifier for the flagged update being decided.",
    )
    action: ReviewAction
    edited_value: Optional[Any] = Field(
        default=None,
        description="New value when action='edit'. Size-bounded (16 KiB).",
    )

    @field_validator("edited_value")
    @classmethod
    def _check_edited_value(cls, v: Any) -> Any:
        return _validate_edited_value(v)


class ReviewSubmission(_BaseModel):
    """POST /review/{patient_id} request body."""

    run_id: str = Field(
        ...,
        pattern=RUN_ID_PATTERN,
        description="The pipeline run the decisions apply to (UUIDv4 format).",
    )
    decisions: list[ReviewDecision] = Field(
        ...,
        min_length=1,
        max_length=MAX_DECISIONS_PER_SUBMISSION,
    )


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #
class UploadUrlResponse(_BaseModel):
    """Response from POST /get-upload-url."""

    upload_url: str = Field(
        ...,
        description="Presigned S3 PUT URL the client uploads the file to.",
    )
    s3_key: str = Field(
        ...,
        description="The S3 key the client echoes back in /intake or /reassessment.",
    )
    expires_in: int = Field(
        ...,
        ge=60,
        le=3600,
        description="Seconds until the presigned URL expires.",
    )


class PipelineResponse(_BaseModel):
    """Response from POST /intake or POST /reassessment."""

    run_id: str = Field(..., pattern=RUN_ID_PATTERN)
    status: PipelineStatus
    message: str = Field(..., max_length=500)


class PatientStatusResponse(_BaseModel):
    """Response from GET /patient/{patient_id}/status."""

    patient_id: str = Field(..., pattern=PATIENT_ID_PATTERN)
    run_id: Optional[str] = Field(default=None, pattern=RUN_ID_PATTERN)
    # "none" distinguishes "patient has never run a pipeline" from "queued".
    status: PipelineStatus | Literal["none"]
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = Field(default=None, max_length=2000)
    has_pending_review: bool = False


class ReviewResponse(_BaseModel):
    """Response from GET /review/{patient_id}."""

    pending: bool
    run_id: Optional[str] = Field(default=None, pattern=RUN_ID_PATTERN)
    # Each flagged update has already been validated as an UpdateObject
    # upstream; at the API boundary we expose them as plain dicts so the
    # Flutter client can render them without importing our internal schema.
    flagged_updates: list[dict[str, Any]] = Field(default_factory=list)
    patient_context: Optional[dict[str, Any]] = Field(default=None)


class DownloadUrlResponse(_BaseModel):
    """Response from GET /get-download-url/{patient_id}."""

    download_url: str
    expires_in: int = Field(..., ge=60, le=3600)
    generated_at: datetime


class HealthResponse(_BaseModel):
    """Response from GET /health."""

    status: str = Field(..., max_length=32)
    version: str = Field(..., max_length=32)
    environment: str = Field(..., max_length=32)


class ErrorResponse(_BaseModel):
    """
    Uniform error body for 4xx/5xx responses.

    `trace_id` is a server-generated correlation ID that clients and
    support can use to find the failing request in logs WITHOUT the
    server revealing stack traces, SQL, or PHI in the response.
    """

    error: str = Field(..., max_length=128)
    trace_id: Optional[str] = Field(default=None, max_length=64)
    detail: Optional[str] = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Internal models (pipeline state + message queue payloads)
# --------------------------------------------------------------------------- #
class UpdateObject(_BaseModel):
    """
    Validated shape of a single LLM-proposed update.

    Produced by `llm_map`, vetted by `llm_critic`, routed by `confidence`.
    Parsing raw LLM output through this model is how we defend against
    hallucinated/malformed JSON.
    """

    field_path: str = Field(
        ...,
        min_length=1,
        max_length=FIELD_PATH_MAX_LEN,
        pattern=FIELD_PATH_PATTERN,
    )
    new_value: Any = Field(
        ...,
        description="LLM-proposed new value. Concrete type depends on field_path.",
    )
    source_phrase: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SOURCE_PHRASE_LEN,
        description="Verbatim phrase from the transcript supporting this update.",
    )
    reasoning: str = Field(
        ...,
        min_length=1,
        max_length=MAX_REASONING_LEN,
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
    )


class SQSMessage(_BaseModel):
    """
    Validated shape of a pipeline job message on the SQS queue.

    The worker constructs this model from the raw message body BEFORE any
    downstream action. Any attacker (or misbehaving internal service) with
    SQS:SendMessage on the queue must not be able to pivot into arbitrary
    DB writes — rigorous validation here is the gate.

    Two flavours of message travel on the queue:

      * Fresh run (`resume=False`, default): the worker builds a new
        `PipelineState` and invokes the graph from START. `decisions`
        MUST be None.
      * Resume (`resume=True`): the worker loads the checkpointed graph
        and resumes it with `Command(resume={"decisions": ...})`.
        `decisions` MUST be a non-empty list; `pipeline_type` MUST be
        'reassessment' because intake has no human-review interrupt.

    The two shapes are cross-checked by a model-level validator so an
    inconsistent body (e.g. `resume=True` with no decisions) is refused
    at the boundary rather than halfway through the worker.
    """

    run_id: str = Field(..., pattern=RUN_ID_PATTERN)
    patient_id: str = Field(
        ...,
        min_length=1,
        max_length=PATIENT_ID_MAX_LEN,
        pattern=PATIENT_ID_PATTERN,
    )
    s3_key: str = Field(
        ...,
        min_length=1,
        max_length=S3_KEY_MAX_LEN,
    )
    pipeline_type: PipelineType
    user_id: str = Field(..., min_length=1, max_length=USER_ID_MAX_LEN)
    resume: bool = Field(
        default=False,
        description=(
            "When True, the worker resumes a paused graph via "
            "Command(resume=...). When False, the worker starts a fresh run."
        ),
    )
    decisions: Optional[list[ReviewDecision]] = Field(
        default=None,
        max_length=MAX_DECISIONS_PER_SUBMISSION,
        description=(
            "Caregiver decisions to feed into the resumed graph. Required "
            "and non-empty when resume=True; forbidden when resume=False."
        ),
    )

    @field_validator("s3_key")
    @classmethod
    def _check_s3_key(cls, v: str) -> str:
        return _validate_s3_key(v)

    @model_validator(mode="after")
    def _check_resume_consistency(self) -> "SQSMessage":
        if self.resume:
            if self.pipeline_type != "reassessment":
                raise ValueError(
                    "resume messages must have pipeline_type='reassessment'"
                )
            if not self.decisions:
                raise ValueError(
                    "resume messages must carry a non-empty decisions list"
                )
        else:
            if self.decisions is not None:
                raise ValueError(
                    "decisions are only allowed on resume messages"
                )
        return self


__all__ = [
    # Literal enums
    "FileType",
    "ReviewAction",
    "PipelineType",
    "PipelineStatus",
    "ApprovalMethod",
    # Constraint constants (re-used in auth.py, endpoint handlers, etc.)
    "PATIENT_ID_PATTERN",
    "PATIENT_ID_MAX_LEN",
    "RUN_ID_PATTERN",
    "FIELD_PATH_PATTERN",
    "FIELD_PATH_MAX_LEN",
    "USER_ID_MAX_LEN",
    "S3_KEY_MAX_LEN",
    "S3_KEY_ALLOWED_PREFIXES",
    # Requests
    "UploadUrlRequest",
    "IntakeRequest",
    "ReassessmentRequest",
    "PatientCreateRequest",
    "PatientPatchRequest",
    "PatientListItem",
    "PatientListResponse",
    "PatientDetailResponse",
    "AuthMeResponse",
    "LoginRequest",
    "LoginResponse",
    "SignedPdfRequest",
    "ReviewDecision",
    "ReviewSubmission",
    # Responses
    "UploadUrlResponse",
    "PipelineResponse",
    "PatientStatusResponse",
    "ReviewResponse",
    "DownloadUrlResponse",
    "HealthResponse",
    "ErrorResponse",
    # Internal
    "UpdateObject",
    "SQSMessage",
]
