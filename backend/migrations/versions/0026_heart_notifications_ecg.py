"""heart_rate_notifications and ecg_recordings tables

Revision ID: 0026_heart_notifications_ecg
Revises: 0025_curate_caffeine_hrv_rmssd
Create Date: 2026-10-19

Apple Watch heart rate notifications (HAE ``data.heartRateNotifications``: high,
low and irregular rhythm alerts) and ECG recordings (``data.ecg``). Neither ships
an entry id. A notification is keyed on ``(start_time, kind, source)``; its
heart-rate samples are summarised (min/avg/max/count), the raw archive keeps
them. An ECG recording is keyed on ``(start_time, source)`` and stored as a
summary only (classification, average heart rate, sampling rate, sample count):
the voltage waveform stays in the raw archive (ARCHITECTURE §10). DDL is
idempotent (guarded by an inspector) like 0023/0024.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_heart_notifications_ecg"
down_revision: str | None = "0025_curate_caffeine_hrv_rmssd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "heart_rate_notifications" not in tables:
        op.create_table(
            "heart_rate_notifications",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("threshold", sa.Float(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.Column("hr_min", sa.Float(), nullable=True),
            sa.Column("hr_avg", sa.Float(), nullable=True),
            sa.Column("hr_max", sa.Float(), nullable=True),
            sa.Column("hr_samples", sa.Integer(), nullable=True),
            sa.UniqueConstraint("start_time", "kind", "source", name="uq_heart_rate_notifications"),
        )
        op.create_index("ix_heart_rate_notifications_start_time", "heart_rate_notifications", ["start_time"])
    if "ecg_recordings" not in tables:
        op.create_table(
            "ecg_recordings",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("classification", sa.Text(), nullable=True),
            sa.Column("average_heart_rate", sa.Float(), nullable=True),
            sa.Column("sampling_frequency", sa.Float(), nullable=True),
            sa.Column("voltage_samples", sa.Integer(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.UniqueConstraint("start_time", "source", name="uq_ecg_recordings"),
        )
        op.create_index("ix_ecg_recordings_start_time", "ecg_recordings", ["start_time"])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "ecg_recordings" in tables:
        op.drop_table("ecg_recordings")
    if "heart_rate_notifications" in tables:
        op.drop_table("heart_rate_notifications")
