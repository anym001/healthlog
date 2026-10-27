"""symptoms and medication_doses tables

Revision ID: 0024_symptoms_medications
Revises: 0023_state_of_mind
Create Date: 2026-10-19

Apple Health symptoms (HAE ``data.symptoms``) and medication dose events
(``data.medications``, iOS 26). Both arrive without an entry id and with
localised values (names, severities, statuses), so each table keeps the raw
value plus a canonical slug (``health_vocab.py``) and is keyed on a natural
key: ``(start_time, name, source)`` for symptoms, ``(medication, dose_time)``
for doses, where ``dose_time`` is the scheduled time if any, else the logging
time. DDL is idempotent (guarded by an inspector) like 0023.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_symptoms_medications"
down_revision: str | None = "0023_state_of_mind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "symptoms" not in tables:
        op.create_table(
            "symptoms",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("symptom", sa.Text(), nullable=True),
            sa.Column("severity_raw", sa.Text(), nullable=True),
            sa.Column("severity", sa.Text(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.Column("user_entered", sa.Boolean(), nullable=True),
            sa.Column("cycle_start", sa.Boolean(), nullable=True),
            sa.UniqueConstraint("start_time", "name", "source", name="uq_symptoms"),
        )
        op.create_index("ix_symptoms_start_time", "symptoms", ["start_time"])
    if "medication_doses" not in tables:
        op.create_table(
            "medication_doses",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("medication", sa.Text(), nullable=False),
            sa.Column("dose_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scheduled_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status_raw", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=True),
            sa.Column("dosage", sa.Float(), nullable=True),
            sa.Column("scheduled_dosage", sa.Float(), nullable=True),
            sa.Column("units", sa.Text(), nullable=True),
            sa.Column("nickname", sa.Text(), nullable=True),
            sa.Column("form", sa.Text(), nullable=True),
            sa.Column("is_archived", sa.Boolean(), nullable=True),
            sa.Column("codings", postgresql.JSONB(), nullable=True),
            sa.UniqueConstraint("medication", "dose_time", name="uq_medication_doses"),
        )
        op.create_index("ix_medication_doses_dose_time", "medication_doses", ["dose_time"])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "medication_doses" in tables:
        op.drop_table("medication_doses")
    if "symptoms" in tables:
        op.drop_table("symptoms")
