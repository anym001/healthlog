"""raw_ingest hypertable + compression policies

Revision ID: 0016_raw_ingest_compression
Revises: 0015_findings_history
Create Date: 2026-07-02

The raw archive grows without bound (every HAE payload, verbatim, forever), so
it becomes a TimescaleDB hypertable with a native compression policy; the
metric_samples hypertable gets one too. Columnar compression cuts repetitive
JSON by an order of magnitude with no data loss — the archive stays fully
queryable and re-derivable.

Hypertables require every unique index to include the partition column, which
forces two constraint changes (applied on plain Postgres as well, so both
backends keep identical semantics):

- the PK becomes (id, received_at) — id stays sequence-generated, so it
  remains unique in practice and keeps serving the re-derive CLI's lookups;
- the global UNIQUE on content_hash becomes a plain index; ingest dedup moved
  from ON CONFLICT to SELECT-then-INSERT (see app/ingest.py, archive_raw).

On plain Postgres (the test suite) the Timescale steps are skipped. DDL is
guarded so a partial/replayed migration is safe. The hypertable conversion is
one-way: downgrade removes the policies and restores the original constraints
only where the table is still a regular one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_raw_ingest_compression"
down_revision: str | None = "0015_findings_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Compress raw payloads after a week (they are only re-read by re-derive
# jobs); keep a comfortable window on samples — HAE re-sends ~2 days and
# upserts into compressed chunks work (TimescaleDB >= 2.11) but are slower.
RAW_COMPRESS_AFTER = "7 days"
SAMPLES_COMPRESS_AFTER = "30 days"


def _timescale_available(conn) -> bool:
    # The extension must be *installed in this database* (0001 creates it when
    # the package is present) — mere availability isn't enough, e.g. when
    # downgrading a schema that was migrated on plain Postgres.
    return conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).first() is not None


def _is_hypertable(conn, table: str) -> bool:
    return (
        conn.execute(
            sa.text("SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = :t"),
            {"t": table},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # --- Constraint rework (both backends) --------------------------------
    # received_at becomes the partition column and part of the PK, so it must
    # be NOT NULL (the server_default means no NULLs exist in practice).
    op.execute("UPDATE raw_ingest SET received_at = now() WHERE received_at IS NULL")
    op.alter_column("raw_ingest", "received_at", nullable=False)

    pk = inspector.get_pk_constraint("raw_ingest")
    if pk and pk.get("constrained_columns") == ["id"]:
        op.drop_constraint(pk["name"] or "raw_ingest_pkey", "raw_ingest", type_="primary")
        op.create_primary_key("pk_raw_ingest", "raw_ingest", ["id", "received_at"])

    uniques = {u["name"] for u in inspector.get_unique_constraints("raw_ingest")}
    if "uq_raw_ingest_hash" in uniques:
        op.drop_constraint("uq_raw_ingest_hash", "raw_ingest", type_="unique")
    # Plain index keeps the dedup lookup (SELECT-then-INSERT) an index scan.
    op.execute("CREATE INDEX IF NOT EXISTS ix_raw_ingest_content_hash ON raw_ingest (content_hash)")

    # --- TimescaleDB: hypertable + compression policies --------------------
    if not _timescale_available(conn):
        return

    conn.execute(
        sa.text("SELECT create_hypertable('raw_ingest', 'received_at', migrate_data => TRUE, if_not_exists => TRUE)")
    )
    conn.execute(
        sa.text(
            "ALTER TABLE raw_ingest SET (timescaledb.compress, timescaledb.compress_orderby = 'received_at DESC')"
        )
    )
    conn.execute(
        sa.text(f"SELECT add_compression_policy('raw_ingest', INTERVAL '{RAW_COMPRESS_AFTER}', if_not_exists => TRUE)")
    )

    # metric_samples has been a hypertable since 0001. segmentby/orderby cover
    # the (time, metric, source) unique target, so idempotent re-ingests keep
    # working against compressed chunks.
    if _is_hypertable(conn, "metric_samples"):
        conn.execute(
            sa.text(
                "ALTER TABLE metric_samples SET (timescaledb.compress, "
                "timescaledb.compress_segmentby = 'metric, source', "
                "timescaledb.compress_orderby = 'time DESC')"
            )
        )
        conn.execute(
            sa.text(
                f"SELECT add_compression_policy('metric_samples', "
                f"INTERVAL '{SAMPLES_COMPRESS_AFTER}', if_not_exists => TRUE)"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _timescale_available(conn):
        conn.execute(sa.text("SELECT remove_compression_policy('metric_samples', if_exists => TRUE)"))
        conn.execute(sa.text("SELECT remove_compression_policy('raw_ingest', if_exists => TRUE)"))

    op.execute("DROP INDEX IF EXISTS ix_raw_ingest_content_hash")

    # The hypertable conversion is one-way; restore the original constraints
    # only on a still-regular table (plain Postgres). 0001's DROP TABLE
    # handles a hypertable fine when downgrading to base.
    if not (_timescale_available(conn) and _is_hypertable(conn, "raw_ingest")):
        inspector = sa.inspect(conn)
        pk = inspector.get_pk_constraint("raw_ingest")
        if pk and pk.get("constrained_columns") == ["id", "received_at"]:
            op.drop_constraint(pk["name"] or "pk_raw_ingest", "raw_ingest", type_="primary")
            op.create_primary_key("raw_ingest_pkey", "raw_ingest", ["id"])
        uniques = {u["name"] for u in inspector.get_unique_constraints("raw_ingest")}
        if "uq_raw_ingest_hash" not in uniques:
            op.create_unique_constraint("uq_raw_ingest_hash", "raw_ingest", ["content_hash"])
        op.alter_column("raw_ingest", "received_at", nullable=True)
