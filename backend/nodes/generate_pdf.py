"""
generate_pdf node  —  PLACEHOLDER.

Position in the graph (last step of both intake and reassessment):
    ... → save_json → audit → GENERATE_PDF → END

Intended behaviour of the REAL implementation:
    * Read `final_json` (reassessment) or `master_json` (intake) from
      state.
    * Render an updated assessment PDF using the facility's approved
      template. Implementation options: server-side HTML→PDF (e.g.
      WeasyPrint) with a hardened template, or a PDF-forms fill step
      using a stored blank form.
    * Upload the rendered bytes to S3 under a `pdfs/` prefix with the
      key shape `pdfs/{patient_id}/{run_id}.pdf`.
    * Set server-side encryption on the upload (SSE-KMS; the bucket
      must already have SSE-KMS as its default, but we set
      `ServerSideEncryption="aws:kms"` explicitly at PutObject time
      as belt-and-suspenders).
    * Return `{"output_pdf_s3_key": "..."}` so the caller can issue a
      short-TTL presigned download URL.

Why this placeholder is a NO-OP rather than a hard failure:
    By the time generate_pdf runs, the durable record of the run is
    already safe:
      * save_json has written the new master JSON to
        `patients.assessment`.
      * audit has written one append-only row per applied update.
    A missing PDF is an annoying artifact gap, not a data-integrity
    issue. Raising here would mark an otherwise-successful run as
    FAILED, hide the fact that the clinical work DID land, and make
    the caregiver re-record their dictation. That is a worse user-
    facing failure than "the generated PDF is unavailable until the
    renderer is implemented."
    The warning log line fires on every run so operators keep visible
    pressure to implement the real renderer.

Security posture (applies to the real implementation):
  * final_json is PHI. The renderer must never log the rendered bytes,
    the intermediate HTML, or any field value. Log the byte size, the
    page count, and the S3 key only.
  * The PDF template must NOT render user-supplied HTML/JS. HTML→PDF
    renderers frequently process whatever markup the input contains;
    a caregiver-edited value that happens to look like `<script>`
    could yield XSS-in-PDF (JavaScript execution in some viewers) or
    exfiltrate through image tags (SSRF via `<img src>`). Escape every
    value before template substitution and disable network access in
    the renderer if possible.
  * The upload must use SSE-KMS. Verify the bucket's default
    encryption during provisioning — `enforce_production_requirements`
    already refuses to boot with a `-dev` bucket in prod.
  * Presigned download URLs are issued outside this node (in the API
    layer), with short TTL and scoped to the exact key. This node
    never issues URLs.
  * Size-cap the rendered PDF before upload (~20 MB should be more
    than enough for an assessment document; refuse anything larger as
    a runaway template symptom).
"""

from __future__ import annotations

import logging
from typing import Any

from state import PipelineState

logger = logging.getLogger(__name__)


async def generate_pdf_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: render the updated assessment as a PDF and upload
    to S3.

    Placeholder: returns `{"output_pdf_s3_key": None}` and emits a
    WARNING-level log line so operators see the gap on every run.
    See module docstring for the decision to no-op rather than raise.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    final_json = state.get("final_json") or state.get("master_json")

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("generate_pdf requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("generate_pdf requires patient_id on state")
    if not isinstance(final_json, dict):
        # We do not emit a PDF, but we still refuse to declare the node
        # successful if the input shape is wrong — a wrong shape at
        # this point means an earlier node misbehaved and we want that
        # visible, not hidden behind a "not implemented" log line.
        raise TypeError(
            "generate_pdf requires final_json (or master_json) as a dict"
        )

    logger.warning(
        "generate_pdf placeholder: run_id=%s patient_id=%s "
        "(PDF renderer not implemented; run will complete without an output PDF)",
        run_id,
        patient_id,
    )

    return {"output_pdf_s3_key": None}


__all__ = ["generate_pdf_node"]
