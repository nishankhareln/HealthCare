"""
SQLAlchemy ORM models for the Samni Labs backend.

Three tables:
  * `patients`       — master record per patient; `assessment` holds the
                        full structured JSON (200+ fields) as JSONB.
  * `audit_trail`    — append-only per-field change log. Every update that
                        reaches the patient JSON writes exactly one row here.
  * `pipeline_runs`  — one row per pipeline execution; tracks lifecycle
                        status, flagged updates awaiting review, and the
                        final PDF S3 key.

Security / data-integrity posture:
  * Every String column has an explicit length. PostgreSQL doesn't gain
    performance from unbounded text, but a length cap is a cheap DoS guard
    — an attacker with write access can't stuff gigabytes into a column.
  * Enum-like fields (pipeline_type, status, approval_method) are protected
    by CHECK constraints so the database itself rejects typos or injected
    values. Python code and SQL-level writes must agree.
  * `confidence` is CHECK-constrained to [0.0, 1.0].
  * FKs use `ON DELETE RESTRICT` — a patient record can never be deleted
    while audit or pipeline history references it. Combined with the
    append-only audit contract, this makes the trail tamper-resistant
    without needing triggers.
  * `id` on audit_trail uses `BigInteger` so the counter cannot wrap at
    ~2.1 billion entries. Facilities generate thousands of rows per patient
    over time; integer overflow would be a real, if slow, risk.
  * `__repr__` on every model redacts everything except identifiers — a
    stray `repr(patient)` in a log must NOT leak the assessment JSONB.
  * No Python-side defaults for user-controlled fields (patient_id, user_id).
    These must be supplied by the caller; defaulting them here would mask
    authz bugs.
  * No relationships() are declared. All joins are explicit in node code,
    which prevents accidental N+1 loads and accidental eager-loading of
    PHI-bearing columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


# --------------------------------------------------------------------------- #
# Enum-like domain constants (mirrored in Pydantic models and CHECK constraints)
# --------------------------------------------------------------------------- #
PIPELINE_TYPE_INTAKE = "intake"
PIPELINE_TYPE_REASSESSMENT = "reassessment"
PIPELINE_TYPES: tuple[str, ...] = (PIPELINE_TYPE_INTAKE, PIPELINE_TYPE_REASSESSMENT)

PIPELINE_STATUS_QUEUED = "queued"
PIPELINE_STATUS_RUNNING = "running"
PIPELINE_STATUS_WAITING_REVIEW = "waiting_review"
PIPELINE_STATUS_COMPLETE = "complete"
PIPELINE_STATUS_FAILED = "failed"
PIPELINE_STATUSES: tuple[str, ...] = (
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_RUNNING,
    PIPELINE_STATUS_WAITING_REVIEW,
    PIPELINE_STATUS_COMPLETE,
    PIPELINE_STATUS_FAILED,
)

APPROVAL_METHOD_AUTO = "auto"
APPROVAL_METHOD_HUMAN = "human"
APPROVAL_METHODS: tuple[str, ...] = (APPROVAL_METHOD_AUTO, APPROVAL_METHOD_HUMAN)


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Render a SQL IN-list from a tuple of allowed values, SQL-quoted."""
    # Values are compile-time string literals defined in this module — there
    # is no user input here. Still, we defensively reject anything with a
    # single quote to preserve the invariant.
    parts: list[str] = []
    for v in values:
        if "'" in v:
            raise ValueError(f"Illegal character in enum value: {v!r}")
        parts.append(f"'{v}'")
    return "(" + ", ".join(parts) + ")"


# --------------------------------------------------------------------------- #
# patients
# --------------------------------------------------------------------------- #
class Patient(Base):
    """
    Master patient record. One row per patient.

    `assessment` is the entire structured JSON document (200+ fields) stored
    as JSONB. Intake upserts this row; reassessment mutates specific keys
    inside `assessment`.
    """

    __tablename__ = "patients"

    patient_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    facility_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    assessment: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
    )
    updated_by: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(patient_id) > 0",
            name="ck_patients_patient_id_nonempty",
        ),
    )

    def __repr__(self) -> str:
        # Deliberately does NOT include `assessment` — that column is PHI.
        return (
            f"<Patient patient_id={self.patient_id!r} "
            f"facility_id={self.facility_id!r} "
            f"updated_at={self.updated_at!r}>"
        )


# --------------------------------------------------------------------------- #
# audit_trail
# --------------------------------------------------------------------------- #
class AuditEntry(Base):
    """
    Append-only per-field change log.

    INVARIANTS (enforced by convention + DB privileges, not ORM):
      * Rows are never UPDATEd or DELETEd by application code.
      * In production, the DB role used by the app must lack UPDATE/DELETE
        grants on this table. See the operations runbook.
    """

    __tablename__ = "audit_trail"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    patient_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "patients.patient_id",
            ondelete="RESTRICT",
            name="fk_audit_trail_patient_id_patients",
        ),
        nullable=False,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    field_path: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    old_value: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    new_value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_phrase: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    confidence: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    approval_method: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"approval_method IN {_sql_in_list(APPROVAL_METHODS)}",
            name="ck_audit_trail_approval_method",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_audit_trail_confidence_range",
        ),
        CheckConstraint(
            "length(patient_id) > 0",
            name="ck_audit_trail_patient_id_nonempty",
        ),
        CheckConstraint(
            "length(run_id) > 0",
            name="ck_audit_trail_run_id_nonempty",
        ),
        CheckConstraint(
            "length(field_path) > 0",
            name="ck_audit_trail_field_path_nonempty",
        ),
        CheckConstraint(
            "length(user_id) > 0",
            name="ck_audit_trail_user_id_nonempty",
        ),
        # Per-patient history queries: "show all changes to this patient,
        # newest first" → (patient_id, created_at DESC).
        Index(
            "ix_audit_trail_patient_id_created_at",
            "patient_id",
            "created_at",
        ),
        # Cross-patient analytics: "how often does this field change?".
        Index(
            "ix_audit_trail_field_path_created_at",
            "field_path",
            "created_at",
        ),
        # "Show all changes produced by one pipeline run."
        Index(
            "ix_audit_trail_run_id_created_at",
            "run_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        # `old_value`, `new_value`, `source_phrase` are PHI — never in repr.
        return (
            f"<AuditEntry id={self.id!r} patient_id={self.patient_id!r} "
            f"run_id={self.run_id!r} field_path={self.field_path!r} "
            f"confidence={self.confidence!r} "
            f"approval_method={self.approval_method!r} "
            f"created_at={self.created_at!r}>"
        )


# --------------------------------------------------------------------------- #
# pipeline_runs
# --------------------------------------------------------------------------- #
class PipelineRun(Base):
    """
    One row per pipeline execution.

    `flagged_updates` is populated when status == 'waiting_review' and is
    consumed by POST /review/{patient_id} to resume the paused LangGraph
    pipeline. Treat it as transient state — once the run is complete or
    failed, its contents are no longer authoritative.
    """

    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    patient_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "patients.patient_id",
            ondelete="RESTRICT",
            name="fk_pipeline_runs_patient_id_patients",
        ),
        nullable=False,
        index=True,
    )
    pipeline_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PIPELINE_STATUS_QUEUED,
        server_default=PIPELINE_STATUS_QUEUED,
    )
    user_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    s3_key: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
    )
    output_pdf_s3_key: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
    )
    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    flagged_updates: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSONB,
        nullable=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            f"pipeline_type IN {_sql_in_list(PIPELINE_TYPES)}",
            name="ck_pipeline_runs_pipeline_type",
        ),
        CheckConstraint(
            f"status IN {_sql_in_list(PIPELINE_STATUSES)}",
            name="ck_pipeline_runs_status",
        ),
        CheckConstraint(
            "length(run_id) > 0",
            name="ck_pipeline_runs_run_id_nonempty",
        ),
        CheckConstraint(
            "length(patient_id) > 0",
            name="ck_pipeline_runs_patient_id_nonempty",
        ),
        CheckConstraint(
            "length(user_id) > 0",
            name="ck_pipeline_runs_user_id_nonempty",
        ),
        # Latest-run-per-patient lookup: used by GET /patient/{id}/status.
        Index(
            "ix_pipeline_runs_patient_id_created_at",
            "patient_id",
            "created_at",
        ),
        # Reviewers' dashboard: "patients with pending reviews".
        Index(
            "ix_pipeline_runs_patient_id_status",
            "patient_id",
            "status",
        ),
    )

    def __repr__(self) -> str:
        # `error`, `flagged_updates`, `s3_key`, `output_pdf_s3_key` omitted —
        # they can carry PHI (error text often quotes transcript fragments;
        # S3 keys embed patient_id).
        return (
            f"<PipelineRun run_id={self.run_id!r} "
            f"patient_id={self.patient_id!r} "
            f"pipeline_type={self.pipeline_type!r} "
            f"status={self.status!r} "
            f"created_at={self.created_at!r}>"
        )


__all__ = [
    "Patient",
    "AuditEntry",
    "PipelineRun",
    # Domain constants — import these in nodes/endpoints instead of hardcoding.
    "PIPELINE_TYPE_INTAKE",
    "PIPELINE_TYPE_REASSESSMENT",
    "PIPELINE_TYPES",
    "PIPELINE_STATUS_QUEUED",
    "PIPELINE_STATUS_RUNNING",
    "PIPELINE_STATUS_WAITING_REVIEW",
    "PIPELINE_STATUS_COMPLETE",
    "PIPELINE_STATUS_FAILED",
    "PIPELINE_STATUSES",
    "APPROVAL_METHOD_AUTO",
    "APPROVAL_METHOD_HUMAN",
    "APPROVAL_METHODS",
]
