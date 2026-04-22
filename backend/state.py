"""
LangGraph pipeline state.

A single TypedDict that flows through every node. `total=False` makes all
keys optional, so each node returns only the fields it wants to merge —
the LangGraph runtime handles the merge. This matches LangGraph's
partial-update convention.

Security posture:
  * This object is the ONE place PHI lives during a pipeline run. The
    transcript, master_json, proposed_updates, audit_entries, etc. are
    all PHI. The LangGraph checkpointer persists this state to
    PostgreSQL — the same tier already trusted with the patient record —
    but it must never leak through logs, error responses, or telemetry.
  * `PHI_FIELDS` lists every PHI-bearing key. Downstream code that logs
    or serializes a state dict MUST redact these. `summarize_state()`
    returns a pre-redacted view and is the ONLY approved way to log
    pipeline state.
  * `new_pipeline_state()` is the single factory. It validates identity
    fields against the same regex/length contracts used by the API layer
    (models.py), so a malformed run_id or patient_id cannot be smuggled
    in by a misbehaving worker.
  * No helper in this module ever logs. Counts can be logged by callers
    using `summarize_state`; content must never be logged.
"""

from __future__ import annotations

import re
from typing import Any, Optional, TypedDict

# db_models owns the domain constants; reusing them prevents string drift
# between the DB CHECK constraints and the pipeline layer.
from db_models import (
    PIPELINE_STATUS_COMPLETE,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_RUNNING,
    PIPELINE_STATUS_WAITING_REVIEW,
    PIPELINE_STATUSES,
    PIPELINE_TYPE_INTAKE,
    PIPELINE_TYPE_REASSESSMENT,
    PIPELINE_TYPES,
)
# Same patterns used by the API request models — validating here keeps the
# pipeline defensive even if state comes from a checkpoint or SQS.
from models import PATIENT_ID_PATTERN, RUN_ID_PATTERN, USER_ID_MAX_LEN
from utils import utc_now_iso


# --------------------------------------------------------------------------- #
# Sentinels / constants
# --------------------------------------------------------------------------- #
# The LLM returns field_path="AMBIGUOUS" when the transcript statement
# cannot be mapped to a concrete field. These updates are auto-flagged for
# human review.
AMBIGUOUS_FIELD_PATH: str = "AMBIGUOUS"

# Keys of PipelineState that carry PHI. Used by summarize_state and can be
# used by any future redaction middleware.
PHI_FIELDS: frozenset[str] = frozenset(
    {
        "master_json",
        "transcript",
        "transcript_segments",
        "raw_pages",
        "proposed_updates",
        "verified_updates",
        "auto_approved",
        "flagged_updates",
        "human_decisions",
        "approved_updates",
        "final_json",
        "audit_entries",
    }
)

# Re-exports so nodes have one import site for status / type constants.
STATUS_QUEUED = PIPELINE_STATUS_QUEUED
STATUS_RUNNING = PIPELINE_STATUS_RUNNING
STATUS_WAITING_REVIEW = PIPELINE_STATUS_WAITING_REVIEW
STATUS_COMPLETE = PIPELINE_STATUS_COMPLETE
STATUS_FAILED = PIPELINE_STATUS_FAILED
PIPELINE_INTAKE = PIPELINE_TYPE_INTAKE
PIPELINE_REASSESSMENT = PIPELINE_TYPE_REASSESSMENT


# --------------------------------------------------------------------------- #
# The state TypedDict
# --------------------------------------------------------------------------- #
class PipelineState(TypedDict, total=False):
    """
    Shared state for the LangGraph pipeline.

    `total=False` means every key is optional. A node receives the full
    current state and returns a partial dict of keys it wants to update.
    Missing keys retain their previous values.

    DO NOT LOG THIS OBJECT. Use `summarize_state()` instead.
    """

    # ---- Identity -------------------------------------------------------- #
    run_id: str
    patient_id: str
    user_id: str
    pipeline_type: str

    # ---- Inputs (S3 keys — files live in S3, not in state) --------------- #
    pdf_s3_key: Optional[str]
    audio_s3_key: Optional[str]

    # ---- Parser output (intake only) ------------------------------------- #
    raw_pages: Optional[list[Any]]

    # ---- Master JSON ----------------------------------------------------- #
    # Loaded from `patients.assessment` at the start of a reassessment run;
    # constructed by parse_pdf for intake.
    master_json: dict[str, Any]

    # ---- Transcription --------------------------------------------------- #
    transcript: Optional[str]
    transcript_segments: Optional[list[Any]]

    # ---- LLM stages ------------------------------------------------------ #
    proposed_updates: Optional[list[dict[str, Any]]]
    verified_updates: Optional[list[dict[str, Any]]]

    # ---- Confidence routing ---------------------------------------------- #
    auto_approved: Optional[list[dict[str, Any]]]
    flagged_updates: Optional[list[dict[str, Any]]]

    # ---- Human review ---------------------------------------------------- #
    human_decisions: Optional[list[dict[str, Any]]]
    approved_updates: Optional[list[dict[str, Any]]]

    # ---- Output ---------------------------------------------------------- #
    final_json: Optional[dict[str, Any]]
    output_pdf_s3_key: Optional[str]
    audit_entries: Optional[list[dict[str, Any]]]

    # ---- Status tracking ------------------------------------------------- #
    status: str
    error: Optional[str]
    started_at: Optional[str]    # ISO 8601 UTC
    completed_at: Optional[str]  # ISO 8601 UTC


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
_PATIENT_ID_RE = re.compile(PATIENT_ID_PATTERN)
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def new_pipeline_state(
    *,
    run_id: str,
    patient_id: str,
    user_id: str,
    pipeline_type: str,
    pdf_s3_key: Optional[str] = None,
    audio_s3_key: Optional[str] = None,
) -> PipelineState:
    """
    Build the initial `PipelineState` for a new run.

    Validates the identity fields against the same contracts the API
    layer enforces. Defense in depth: even if state construction somehow
    bypassed the normal HTTP path (e.g. a rehydrated checkpoint or a
    future admin CLI), we still reject malformed identifiers here.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError("run_id must be a UUIDv4-format string")
    if not isinstance(patient_id, str) or not _PATIENT_ID_RE.match(patient_id):
        raise ValueError("patient_id does not match the allowed pattern")
    if not isinstance(user_id, str) or not user_id or len(user_id) > USER_ID_MAX_LEN:
        raise ValueError("user_id must be a non-empty string under the length cap")
    if pipeline_type not in PIPELINE_TYPES:
        raise ValueError(f"pipeline_type must be one of {PIPELINE_TYPES}")

    # Per-type required input guardrail — prevents a reassessment run from
    # silently proceeding with no audio, or an intake with no PDF.
    if pipeline_type == PIPELINE_TYPE_INTAKE and not pdf_s3_key:
        raise ValueError("intake pipeline requires pdf_s3_key")
    if pipeline_type == PIPELINE_TYPE_REASSESSMENT and not audio_s3_key:
        raise ValueError("reassessment pipeline requires audio_s3_key")

    state: PipelineState = {
        "run_id": run_id,
        "patient_id": patient_id,
        "user_id": user_id,
        "pipeline_type": pipeline_type,
        "pdf_s3_key": pdf_s3_key,
        "audio_s3_key": audio_s3_key,
        "master_json": {},
        "status": STATUS_RUNNING,
        "started_at": utc_now_iso(),
        "completed_at": None,
        "error": None,
    }
    return state


# --------------------------------------------------------------------------- #
# Status transitions
# --------------------------------------------------------------------------- #
def mark_status(
    state: PipelineState,
    status_value: str,
    *,
    error: Optional[str] = None,
) -> PipelineState:
    """
    Return a shallow-copied state with `status` (and optionally `error`,
    `completed_at`) updated.

    Does NOT mutate `state` — callers return the result to LangGraph.
    """
    if status_value not in PIPELINE_STATUSES:
        raise ValueError(f"status must be one of {PIPELINE_STATUSES}")

    new_state: PipelineState = dict(state)  # type: ignore[assignment]
    new_state["status"] = status_value
    if status_value in (STATUS_COMPLETE, STATUS_FAILED):
        new_state["completed_at"] = utc_now_iso()
    if error is not None:
        # Bound the size so an error from deep in the stack can't bloat the
        # checkpoint. Truncate with a marker rather than silently dropping.
        max_len = 2000
        safe = error if len(error) <= max_len else error[: max_len - 12] + "...truncated"
        new_state["error"] = safe
    return new_state


# --------------------------------------------------------------------------- #
# PHI-safe state logging
# --------------------------------------------------------------------------- #
def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def summarize_state(state: PipelineState | dict[str, Any]) -> dict[str, Any]:
    """
    Produce a PHI-safe dict suitable for logging.

    Returns only identifiers, booleans, and counts. Every field listed in
    `PHI_FIELDS` is collapsed to a length/presence indicator. No value,
    key name, or field path from the patient JSON leaks out.

    This is the ONLY approved way to log pipeline state.
    """
    # Access via `.get` so the function is safe to call on a raw dict or a
    # partially-populated TypedDict.
    get = state.get  # type: ignore[assignment]

    return {
        "run_id": get("run_id"),
        "patient_id": get("patient_id"),
        "pipeline_type": get("pipeline_type"),
        "status": get("status"),
        "started_at": get("started_at"),
        "completed_at": get("completed_at"),
        "has_error": get("error") is not None,
        "has_pdf_key": get("pdf_s3_key") is not None,
        "has_audio_key": get("audio_s3_key") is not None,
        "has_output_pdf_key": get("output_pdf_s3_key") is not None,
        "has_transcript": get("transcript") is not None,
        "master_json_populated": bool(get("master_json")),
        "proposed_count": _list_len(get("proposed_updates")),
        "verified_count": _list_len(get("verified_updates")),
        "auto_approved_count": _list_len(get("auto_approved")),
        "flagged_count": _list_len(get("flagged_updates")),
        "human_decisions_count": _list_len(get("human_decisions")),
        "approved_count": _list_len(get("approved_updates")),
        "audit_entries_count": _list_len(get("audit_entries")),
        "raw_pages_count": _list_len(get("raw_pages")),
        "transcript_segments_count": _list_len(get("transcript_segments")),
    }


__all__ = [
    # The state itself
    "PipelineState",
    "new_pipeline_state",
    "mark_status",
    "summarize_state",
    # Constants
    "AMBIGUOUS_FIELD_PATH",
    "PHI_FIELDS",
    # Re-exported status / type constants
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_WAITING_REVIEW",
    "STATUS_COMPLETE",
    "STATUS_FAILED",
    "PIPELINE_INTAKE",
    "PIPELINE_REASSESSMENT",
]
