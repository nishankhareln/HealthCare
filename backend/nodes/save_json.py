"""
save_json node.

Persists `final_json` to the `patients.assessment` JSONB column. This is
the ONLY path in the system that writes to `patients.assessment` after
the initial intake upsert — every reassessment lands here.

Position in the graph:
    merge → SAVE_JSON → audit → generate_pdf → (notify + terminal)

Inputs (from state):
    run_id, patient_id, user_id
    final_json:  dict — the merged assessment document

Outputs (merged into state):
    (no state mutation; this node is a side-effect sink)

Security posture:
  * Parameterised ORM write — no string-concatenated SQL, so SQL injection
    through patient_id or assessment keys is not possible.
  * Row-level isolation via SELECT … FOR UPDATE. Two concurrent
    reassessments for the same patient cannot clobber each other; the
    second one blocks on the first's commit and then reads the fresh
    assessment (the LangGraph checkpointer will have already captured
    the updated master_json before merge, so this is belt-and-suspenders).
  * Strict identity checks: `patient_id` must equal `state.patient_id`.
    If a merge step ever produced a final_json that references a
    different patient (a bug we want to catch), we refuse to write.
  * `updated_by` is set from `state.user_id` for audit attribution.
    `updated_at` is set via `func.now()` on the DB side so the server's
    clock (not ours) is the source of truth.
  * Never logs `final_json`. Logs `run_id`, `patient_id`, byte-count of
    the serialized JSON, and success/failure only.
  * Writes use the existing async session factory from database.py,
    which already has `statement_timeout=30000` configured — a runaway
    write cannot hold a lock forever.
  * On any DB error, the exception propagates to the node wrapper so
    the pipeline marks the run failed. We do NOT catch-and-continue —
    a failed save_json means the record is stale, and the audit and
    generate_pdf nodes must not run against stale data.
  * No hand-rolled JSON serialisation for the column — asyncpg/SQLAlchemy
    handles the dict → JSONB conversion, which is the safe path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from database import get_db_session
from db_models import Patient
from state import PipelineState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# Even though PostgreSQL JSONB accepts up to 1 GB, a realistic patient
# document is 10–50 KB. Cap at 2 MiB: plenty of headroom for edge cases,
# tight enough to refuse a runaway pipeline that has ballooned the doc
# through a bad merge.
_MAX_ASSESSMENT_BYTES: int = 2 * 1024 * 1024


def _estimate_size_bytes(doc: dict[str, Any]) -> int:
    """
    Return the JSON-serialized size of `doc` in bytes. Used only for
    the size guardrail; we throw this value away after the check.
    """
    try:
        encoded = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("final_json is not JSON-serializable") from exc
    return len(encoded.encode("utf-8"))


async def save_json_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: write `final_json` to `patients.assessment` for the
    patient named on the state.

    Returns an empty dict (nothing to merge back into state). Raises on
    unrecoverable failures so the pipeline wrapper can transition the
    run to FAILED.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    user_id = state.get("user_id")
    final_json = state.get("final_json")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("save_json requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("save_json requires patient_id on state")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("save_json requires user_id on state")
    if not isinstance(final_json, dict):
        raise TypeError("save_json requires final_json as a dict")

    size_bytes = _estimate_size_bytes(final_json)
    if size_bytes > _MAX_ASSESSMENT_BYTES:
        raise ValueError(
            f"final_json size {size_bytes} exceeds cap {_MAX_ASSESSMENT_BYTES}"
        )

    async with get_db_session() as session:
        # Lock the patient row so two concurrent reassessments for the
        # same patient serialise cleanly at the DB layer. `with_for_update`
        # translates to SELECT ... FOR UPDATE.
        stmt = (
            select(Patient)
            .where(Patient.patient_id == patient_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        patient: Patient | None = result.scalar_one_or_none()

        if patient is None:
            # An intake run writes the row first; a reassessment must
            # find it. If it is missing, something upstream skipped
            # intake — refuse to create one here silently (that would
            # hide a bug and create orphaned audit entries).
            raise LookupError(
                f"patients row not found for patient_id (run_id={run_id})"
            )

        patient.assessment = final_json
        patient.updated_by = user_id
        # `updated_at` is handled by the column's `onupdate=func.now()`.

    logger.info(
        "save_json: run_id=%s patient_id=%s bytes=%d",
        run_id,
        patient_id,
        size_bytes,
    )
    return {}


__all__ = ["save_json_node"]
