"""
parse_doctor_pdf node — extract the medical section of the assessment
JSON from a doctor-supplied PDF.

Position in the graph (intake_doctor pipeline):
    parse_doctor_pdf → save_json → audit → END

How this differs from `parse_pdf`:

  * `parse_pdf` (used by the regular intake) processes the 36-page DSHS
    Assessment Details PDF. It owns demographics, mobility, ADLs,
    behaviors, social information, etc.

  * `parse_doctor_pdf` (this node) processes the ~15-page doctor PDF
    (only ~6 pages of useful content). It owns the medical sections —
    diseases, diagnoses, medications, conditions, treatments — and
    nothing else.

Both pipelines write into the same `patients.assessment` JSONB column
but into DIFFERENT keys. We never let the doctor PDF overwrite a
caregiver's mobility entry, and we never let the caregiver PDF
overwrite a doctor's medication list.

Implementation sketch (placeholder for now):

    1. Read the doctor PDF from `pdf_s3_key` in S3.
    2. Send to Bedrock Claude Sonnet 4.5 with a doctor-specific prompt
       that extracts ONLY the medical schema fields:
           - medical.diagnoses[]
           - medical.diseases[]
           - medical.medications[]
           - medical.conditions[]
           - medical.allergies[]
           - medical.history[]
    3. Read the patient's existing assessment from Postgres.
    4. Deep-merge the medical extraction INTO the existing assessment.
       Non-medical keys are preserved verbatim.
    5. Build audit_entries — one per CHANGED medical field — so the
       audit table records what the doctor PDF added or updated.
    6. Return the merged dict as `master_json` and the audit list as
       `audit_entries`.

Status today: PLACEHOLDER. Returns a minimal master_json/audit pair so
the graph wiring works end-to-end. Real parsing requires (a) the
Bedrock prompt + JSON schema for the medical sections, and (b) sample
doctor PDFs for the prompt to be tuned against. Both are tracked
elsewhere.

Security posture (applies to the real implementation):
  * The doctor PDF contains PHI. The parser must never log raw page
    text, raw Bedrock responses, or extracted field values. Counts and
    IDs only.
  * Bedrock runs at temperature 0 with a strict JSON schema. Anything
    Claude tries to output outside that schema is rejected by Pydantic
    validation, never silently saved.
  * The merge step uses `set_nested(...)` from utils, which refuses
    paths containing `..`, `__`, or other prototype-pollution patterns.
  * If the doctor PDF refers to a different patient than the one in
    the request (name mismatch, ACES ID mismatch), the node MUST
    abort with an error rather than overwrite the wrong record.
"""

from __future__ import annotations

import logging
from typing import Any

from models import S3_KEY_ALLOWED_PREFIXES
from state import PipelineState

logger = logging.getLogger(__name__)


async def parse_doctor_pdf_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: parse a doctor PDF into the medical sections of the
    patient's assessment.

    Placeholder. Real implementation: Bedrock Sonnet 4.5 multimodal
    call + deep-merge of medical fields into existing assessment.
    """
    run_id = state.get("run_id")
    pdf_s3_key = state.get("pdf_s3_key")
    patient_id = state.get("patient_id")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("parse_doctor_pdf requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("parse_doctor_pdf requires patient_id on state")
    if not isinstance(pdf_s3_key, str) or not pdf_s3_key:
        raise ValueError("parse_doctor_pdf requires pdf_s3_key on state")
    if not any(pdf_s3_key.startswith(p) for p in S3_KEY_ALLOWED_PREFIXES):
        raise ValueError("pdf_s3_key uses a disallowed S3 prefix")

    logger.warning(
        "parse_doctor_pdf placeholder: run_id=%s patient_id=%s "
        "(doctor PDF parser not implemented; producing empty merge)",
        run_id,
        patient_id,
    )

    # Placeholder: return an empty medical block so save_json + audit
    # can complete without crashing. The real implementation will
    # populate `master_json` with the merged JSON and `audit_entries`
    # with the per-field changes.
    return {
        "master_json": {},
        "audit_entries": [],
    }


__all__ = ["parse_doctor_pdf_node"]
