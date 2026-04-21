"""
load_patient_json node.

First node of the REASSESSMENT graph. Loads the patient's current master
JSON from `patients.assessment` and attaches it to state as
`master_json`. Every downstream node (mapper, critic, merge, save_json)
depends on this being present.

Position in the REASSESSMENT graph:
    START → LOAD_PATIENT_JSON → transcribe → llm_map → llm_critic →
            confidence → human_review? → merge → save_json → audit →
            generate_pdf → END

For the INTAKE graph, master_json is built by parse_pdf from the source
document, so this node is skipped there.

Inputs (from state):
    run_id, patient_id — identifiers

Outputs (merged into state):
    master_json: dict — the patient's current assessment

Security posture:
  * Read-only transaction. We do NOT hold a row lock here; the save_json
    node takes the write lock when it commits merged_json at the end.
    Holding a row lock across LLM calls (which can take 30+ seconds)
    would block the whole patient for every reassessment.
  * Parameterised ORM query — patient_id is a bind parameter, never
    interpolated into SQL.
  * patient_id is re-validated against the API-layer regex before the
    query. The ORM would reject a bad value, but failing early keeps
    error messages clean and avoids even touching the DB with garbage
    input.
  * `LookupError` on missing patient — the caller (the LangGraph node
    runner) catches this and transitions the run to FAILED. A missing
    patient row at reassessment time almost always means the intake
    was never run; we want that visible, not silently papered over.
  * The `assessment` column is PHI. We never log its content, keys, or
    length. We log run_id, patient_id, and a boolean "loaded".
  * Deep-copy the loaded dict before placing it on state. asyncpg
    returns a fresh dict per query, but the defensive copy is cheap
    and eliminates any possibility that a downstream mutation reaches
    back into the ORM identity map or a shared cache.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from sqlalchemy import select

from database import get_db_session
from db_models import Patient
from models import PATIENT_ID_PATTERN
from state import PipelineState

logger = logging.getLogger(__name__)

_PATIENT_ID_RE: re.Pattern[str] = re.compile(PATIENT_ID_PATTERN)


async def load_patient_json_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: fetch `patients.assessment` for `state.patient_id`
    and return it as `master_json`.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("load_patient_json requires run_id on state")
    if not isinstance(patient_id, str) or not _PATIENT_ID_RE.match(patient_id):
        # Defence in depth. Validated at the API boundary and again
        # in new_pipeline_state; revalidating here costs a regex and
        # protects against future code paths that construct state
        # without going through the factory.
        raise ValueError("patient_id does not match the allowed pattern")

    async with get_db_session() as session:
        stmt = (
            select(Patient.assessment)
            .where(Patient.patient_id == patient_id)
        )
        result = await session.execute(stmt)
        row = result.first()

    if row is None:
        # The reassessment flow assumes the intake has already produced
        # a row. Missing row → upstream bug or wrong patient_id. Raise
        # so the run is marked FAILED rather than silently continuing
        # with an empty document (which would cause the mapper to
        # invent fields from scratch — a worse failure mode).
        raise LookupError(
            f"patients row not found for patient_id (run_id={run_id})"
        )

    assessment = row[0]
    if not isinstance(assessment, dict):
        # The column is NOT NULL and defined as JSONB; if we ever see a
        # non-dict, something violated the schema assumption. Refuse.
        raise TypeError(
            "patients.assessment is not a JSON object"
        )

    master_json = copy.deepcopy(assessment)

    logger.info(
        "patient json loaded: run_id=%s patient_id=%s",
        run_id,
        patient_id,
    )

    return {"master_json": master_json}


__all__ = ["load_patient_json_node"]
