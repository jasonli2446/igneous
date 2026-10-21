"""Index bounded temporal-edge backfills.

Revision ID: 0003
Revises: 0002

Temporal values are backfilled by the spine in small, idempotent batches rather
than during this migration, so deploys do not take a long table-wide write lock.
"""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("""
        CREATE INDEX IF NOT EXISTS edges_temporal_missing_idx ON edges (id)
        WHERE valid_from IS NULL OR valid_to IS NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS edges_temporal_missing_idx")
