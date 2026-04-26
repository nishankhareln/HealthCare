"""add signed columns

Adds three columns to support the Flutter-rendered / caregiver-signed PDF
archive flow:

  * patients.template_version        — which Flutter template version was
                                        used to render the signed plan.
  * pipeline_runs.signed_pdf_s3_key  — S3 key of the uploaded signed PDF.
  * pipeline_runs.signed_at          — when the signed PDF was registered.

IMPORTANT — this migration is hand-edited after autogenerate.
Autogenerate proposed dropping the LangGraph checkpointer tables
(`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`) because they live in the same database but are
NOT declared in our SQLAlchemy metadata. Those tables are managed at
runtime by `AsyncPostgresSaver.setup()` (see pipeline.py). Dropping them
here would delete every pending human-review pause. Removed.

Revision ID: 61f2ca418acc
Revises: 262038975ff9
Create Date: 2026-04-24 11:14:21.611269+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "61f2ca418acc"
down_revision: Union[str, None] = "262038975ff9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("template_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("signed_pdf_s3_key", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "signed_at")
    op.drop_column("pipeline_runs", "signed_pdf_s3_key")
    op.drop_column("patients", "template_version")
