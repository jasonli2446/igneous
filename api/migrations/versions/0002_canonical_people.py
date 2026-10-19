"""Canonical identity per node: connected components over SAME_AS, refreshed by `igneous resolve`.

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("""
    CREATE TABLE canonical (
        node_id       bigint PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
        canonical_id  bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE
    );
    CREATE INDEX ON canonical (canonical_id);
    """)


def downgrade():
    op.execute("DROP TABLE canonical")
