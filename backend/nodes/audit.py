"""
Audit node.

Persists the in-memory `audit_entries` list (built by merge_node) to the
`audit_trail` table as an append-only log. Runs AFTER save_json so a row
in audit_trail is only ever created for a state that was actually
written to `patients.assessment`.

Position in the graph:
    merge → save_json → AUDIT → generate_pdf

Inputs (from state):
    audit_entries: list[dict] — as produced by merge_node
    run_id / patient_id / user_id — identifiers

Outputs (merged into state):
    (no state mutation; this node is a side-effect sink)

Security posture:
  * Append-only. We only INSERT. The application role on the DB should
    lack UPDATE/DELETE grants on `audit_trail` (documented in the
    operations runbook); this code enforces the same invariant at the
    application layer — there is NO path here that updates or deletes
    a row.
  * Only entries with `status == "applied"` reach the DB. Skipped
    entries (validation failure, ambiguous path, apply error) are
    counted for observability but not written — the audit table is a
    record of ACTUAL changes to the patient record, and a skipped
    update did not change anything. The summary log line still
    surfaces the skipped count so it is not invisible.
  * DB CHECK constraints reject malformed rows (nonempty field_path,
    confidence range, approval_method in allowlist). We add python-
    side validation too so we fail fast with a clean message rather
    than getting a cryptic CHECK violation at commit time.
  * Single transaction with `add_all` + commit. If the insert fails,
    NOTHING is persisted and the whole pipeline run transitions to
    FAILED — partial audit writes would be worse than none (they would
    make the history misleading).
  * Parameterised ORM inserts — no SQL injection possible through any
    user-supplied field (field_path, source_phrase, new_value).
  * Never logs field_path, old_value, new_value, or source_phrase. The
    values being written ARE PHI. They live in audit_trail (the same
    tier as patients.assessment) and nowhere else.
  * `old_value`, `new_value`, and `source_phrase` are Text columns, so
    dict/list values are serialized via `json.dumps` before insert.
    We refuse to insert if serialization fails rather than calling
    `str()` on an arbitrary object (which can silently produce a
    useless representation like `<object at 0x...>`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db_models import (
    APPROVAL_METHODS,
    AuditEntry,
)
from database import get_db_session
from state import PipelineState

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety bounds
# --------------------------------------------------------------------------- #
# Mirrors the merge-time cap. Audit should never receive more entries
# than the merge produced, but we guard against a malformed state where
# audit_entries has grown unexpectedly.
_MAX_AUDIT_ROWS_PER_RUN: int = 1000

# Column string lengths from db_models. Duplicated here (small, stable)
# rather than imported as constants to keep the node standalone.
_FIELD_PATH_MAX: int = 255
_USER_ID_MAX: int = 128
_RUN_ID_MAX: int = 64
_PATIENT_ID_MAX: int = 64
_APPROVAL_METHOD_MAX: int = 16

# Text columns don't have a hard cap at the DB, but we cap at 64 KiB to
# keep a runaway LLM value from blowing up a single row. If someone
# eventually dictates a paragraph as `new_value`, 64 KiB is still 10x
# what a realistic record holds.
_TEXT_COLUMN_MAX_BYTES: int = 64 * 1024


# --------------------------------------------------------------------------- #
# Value serialisation for Text columns
# --------------------------------------------------------------------------- #
def _to_text_column(value: Any) -> Optional[str]:
    """
    Convert a Python value into a Text-safe string.

    - None → None (NULL in the DB; old_value and source_phrase allow it)
    - str  → used as-is
    - bool / int / float → str()
    - dict / list → json.dumps (deterministic sort; separators compact)
    - anything else → ValueError. We refuse rather than `str()` an
      arbitrary object because that silently loses structure and can
      leak object-identity (memory-address repr) into the audit log.
    """
    if value is None:
        return None
    if isinstance(value, str):
        encoded = value
    elif isinstance(value, bool) or isinstance(value, (int, float)):
        encoded = str(value)
    elif isinstance(value, (dict, list)):
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("audit value is not JSON-serializable") from exc
    else:
        raise ValueError(
            f"audit value has unsupported type {type(value).__name__}"
        )

    if len(encoded.encode("utf-8")) > _TEXT_COLUMN_MAX_BYTES:
        raise ValueError(
            f"audit text column exceeds {_TEXT_COLUMN_MAX_BYTES} bytes"
        )
    return encoded


def _bounded_str(value: Any, *, max_len: int, field: str) -> str:
    """
    Coerce `value` to a non-empty string within `max_len` characters, or
    raise. Used for the small identifier columns (patient_id, run_id,
    user_id, approval_method, field_path). NEVER echoes the value in
    the error message.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"audit row: {field} must be a non-empty string")
    if len(value) > max_len:
        raise ValueError(f"audit row: {field} exceeds {max_len} characters")
    return value


def _bounded_confidence(value: Any) -> Optional[float]:
    """
    Coerce to a float in [0.0, 1.0] or None. A bool slips through
    isinstance(value, (int, float)) in Python, so reject it explicitly.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("audit row: confidence must be a number, not a bool")
    if not isinstance(value, (int, float)):
        raise ValueError("audit row: confidence must be a number")
    f = float(value)
    if f < 0.0 or f > 1.0:
        raise ValueError("audit row: confidence out of [0.0, 1.0]")
    return f


# --------------------------------------------------------------------------- #
# Build an ORM row from an in-memory entry
# --------------------------------------------------------------------------- #
def _build_audit_row(
    entry: dict[str, Any],
    *,
    run_id: str,
    patient_id: str,
    user_id: str,
) -> AuditEntry:
    """
    Convert one in-memory audit dict (from merge_node) into an
    `AuditEntry` ORM object ready for `session.add`. Validates every
    field before it touches SQLAlchemy.

    `run_id`, `patient_id`, and `user_id` from the state are used as
    fallbacks / overrides — the entry's own copies must match.
    """
    entry_run_id = _bounded_str(
        entry.get("run_id") or run_id, max_len=_RUN_ID_MAX, field="run_id"
    )
    if entry_run_id != run_id:
        # Entry belongs to a different run. This should be impossible
        # (merge built it this pass) but refusing protects against a
        # future bug that reuses state lists across runs.
        raise ValueError("audit row: run_id mismatch against state")

    entry_patient_id = _bounded_str(
        entry.get("patient_id") or patient_id,
        max_len=_PATIENT_ID_MAX,
        field="patient_id",
    )
    if entry_patient_id != patient_id:
        raise ValueError("audit row: patient_id mismatch against state")

    entry_user_id = _bounded_str(
        entry.get("user_id") or user_id,
        max_len=_USER_ID_MAX,
        field="user_id",
    )

    field_path = _bounded_str(
        entry.get("field_path"),
        max_len=_FIELD_PATH_MAX,
        field="field_path",
    )

    approval_method = _bounded_str(
        entry.get("approval_method"),
        max_len=_APPROVAL_METHOD_MAX,
        field="approval_method",
    )
    if approval_method not in APPROVAL_METHODS:
        raise ValueError(
            f"audit row: approval_method must be one of {APPROVAL_METHODS}"
        )

    new_value_text = _to_text_column(entry.get("new_value"))
    if new_value_text is None:
        # Column is NOT NULL. An applied row must have a concrete
        # new_value; merge_node only marks status=applied on success.
        raise ValueError("audit row: new_value must not be null")

    old_value_text = _to_text_column(entry.get("old_value"))
    source_phrase_text = _to_text_column(entry.get("source_phrase"))
    confidence = _bounded_confidence(entry.get("confidence"))

    return AuditEntry(
        patient_id=entry_patient_id,
        run_id=entry_run_id,
        user_id=entry_user_id,
        field_path=field_path,
        old_value=old_value_text,
        new_value=new_value_text,
        source_phrase=source_phrase_text,
        confidence=confidence,
        approval_method=approval_method,
        # created_at is server_default=func.now().
    )


# --------------------------------------------------------------------------- #
# Node entry point
# --------------------------------------------------------------------------- #
async def audit_node(state: PipelineState) -> dict[str, Any]:
    """
    LangGraph node: persist applied audit entries to `audit_trail`.

    Runs only after save_json. Skipped entries are not written — see
    module docstring for the rationale.

    Returns an empty dict (no state mutation). Raises on failure so the
    pipeline wrapper transitions the run to FAILED.
    """
    run_id = state.get("run_id")
    patient_id = state.get("patient_id")
    user_id = state.get("user_id")
    audit_entries = state.get("audit_entries") or []

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("audit requires run_id on state")
    if not isinstance(patient_id, str) or not patient_id:
        raise ValueError("audit requires patient_id on state")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("audit requires user_id on state")
    if not isinstance(audit_entries, list):
        raise TypeError("audit_entries must be a list")

    total = len(audit_entries)
    if total > _MAX_AUDIT_ROWS_PER_RUN:
        raise ValueError(
            f"audit received {total} entries, exceeds cap of {_MAX_AUDIT_ROWS_PER_RUN}"
        )

    # Partition into applied (persist) and skipped (log-only).
    applied_entries: list[dict[str, Any]] = []
    skipped_count = 0
    for entry in audit_entries:
        if not isinstance(entry, dict):
            # Ignore malformed entries rather than aborting — merge_node
            # should never produce these; if it does, that is a bug we
            # will see via the skipped_count > 0 in a run that thinks it
            # applied everything.
            skipped_count += 1
            continue
        status = entry.get("status")
        if status == "applied":
            applied_entries.append(entry)
        else:
            skipped_count += 1

    if not applied_entries:
        # Nothing to write. Legitimate when a run's updates were all
        # flagged and then rejected by the reviewer.
        logger.info(
            "audit: run_id=%s patient_id=%s inserted=0 skipped=%d",
            run_id,
            patient_id,
            skipped_count,
        )
        return {}

    rows: list[AuditEntry] = [
        _build_audit_row(
            entry,
            run_id=run_id,
            patient_id=patient_id,
            user_id=user_id,
        )
        for entry in applied_entries
    ]

    async with get_db_session() as session:
        session.add_all(rows)
        # The context manager commits on exit; if any row fails a
        # CHECK constraint the whole transaction is rolled back.

    logger.info(
        "audit: run_id=%s patient_id=%s inserted=%d skipped=%d",
        run_id,
        patient_id,
        len(rows),
        skipped_count,
    )
    return {}


__all__ = ["audit_node"]
