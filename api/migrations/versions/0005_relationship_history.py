"""Add claim-oriented node types for relationship history.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    # Node/edge types are deliberately open-ended. This partial index documents and
    # accelerates the product read path without encoding private source content.
    op.execute("""
        CREATE INDEX relationship_history_claim_nodes_idx ON nodes (type, source_item_id)
        WHERE type IN ('Commitment', 'Decision');
        CREATE INDEX relationship_history_edges_idx ON edges (type, source_item_id, valid_from)
        WHERE type IN ('HAS_COMMITMENT', 'HAS_DECISION', 'INTRODUCED');
        CREATE INDEX relationship_history_participants_idx ON edges (source_item_id, type, valid_from)
        WHERE type IN ('SENT', 'RECEIVED');
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS relationship_history_participants_idx;")
    op.execute("DROP INDEX IF EXISTS relationship_history_edges_idx;")
    op.execute("DROP INDEX IF EXISTS relationship_history_claim_nodes_idx;")
